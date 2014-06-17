from collections import namedtuple, defaultdict
import inspect, textwrap, ast

from artiq.compiler.tools import eval_ast
from artiq.language import experiment, units

def _value_to_ast(value):
	if isinstance(value, int):
		return ast.Num(value)
	elif isinstance(value, str):
		return ast.Str(value)
	else:
		for kg in experiment.kernel_globals:
			if value is getattr(experiment, kg):
				return ast.Name(kg, ast.Load())
		if isinstance(value, units.Quantity):
			return ast.Call(
					func=ast.Name("Quantity", ast.Load()),
					args=[ast.Num(value.amount), ast.Name("base_"+value.unit.name+"_unit", ast.Load())],
					keywords=[], starargs=None, kwargs=None)
		return None

def _replace_global(obj, ref):
	try:
		value = eval_ast(ref, inspect.getmodule(obj).__dict__)
	except:
		return None
	return _value_to_ast(value)

_UserVariable = namedtuple("_UserVariable", "name")

class _ReferenceManager:
	def __init__(self):
		# (id(obj), funcname, local) -> _UserVariable(name) / ast / constant_object
		self.to_inlined = dict()
		# inlined_name -> use_count
		self.use_count = dict()
		self.rpc_map = defaultdict(lambda: len(self.rpc_map))

		# reserved names
		self.use_count["Quantity"] = 1
		self.use_count["base_s_unit"] = 1
		self.use_count["base_Hz_unit"] = 1
		for kg in experiment.kernel_globals:
			self.use_count[kg] = 1
		self.use_count["range"] = 1

	def new_name(self, base_name):
		if base_name[-1].isdigit():
			base_name += "_"
		if base_name in self.use_count:
			r = base_name + str(self.use_count[base_name])
			self.use_count[base_name] += 1
			return r
		else:
			self.use_count[base_name] = 1
			return base_name

	def get(self, obj, funcname, ref):
		store = isinstance(ref.ctx, ast.Store)

		if isinstance(ref, ast.Name):
			key = (id(obj), funcname, ref.id)
			try:
				ival = self.to_inlined[key]
			except KeyError:
				if store:
					iname = self.new_name(ref.id)
					self.to_inlined[key] = _UserVariable(iname)
					return ast.Name(iname, ast.Store())
			else:
				if isinstance(ival, _UserVariable):
					return ast.Name(ival.name, ref.ctx)
				elif isinstance(ival, ast.AST):
					assert(not store)
					return ival
				else:
					if store:
						raise NotImplementedError("Cannot turn object into user variable")
					else:
						a = _value_to_ast(ival)
						if a is None:
							raise NotImplementedError("Cannot represent inlined value")
						return a

		if not store:
			repl = _replace_global(obj, ref)
			if repl is not None:
				return repl
		
		raise KeyError

	def set(self, obj, funcname, name, value):
		self.to_inlined[(id(obj), funcname, name)] = value

	def get_constants(self, r_obj, r_funcname):
		return {local: v for (objid, funcname, local), v
			in self.to_inlined.items()
			if objid == id(r_obj)
				and funcname == r_funcname
				and not isinstance(v, (_UserVariable, ast.AST))}

_embeddable_calls = {
	units.Quantity,
	experiment.delay, experiment.at, experiment.now, experiment.syscall,
	range
}

class _ReferenceReplacer(ast.NodeTransformer):
	def __init__(self, rm, obj, funcname):
		self.rm = rm
		self.obj = obj
		self.funcname = funcname
		self.module = inspect.getmodule(self.obj)

	def visit_ref(self, node):
		return self.rm.get(self.obj, self.funcname, node)

	visit_Name = visit_ref
	visit_Attribute = visit_ref
	visit_Subscript = visit_ref

	def visit_Call(self, node):
		calldict = self.rm.get_constants(self.obj, self.funcname)
		calldict.update(self.module.__dict__)
		func = eval_ast(node.func, calldict)

		new_args = [self.visit(arg) for arg in node.args]

		if func in _embeddable_calls:
			new_func = ast.Name(func.__name__, ast.Load())
			return ast.Call(func=new_func, args=new_args,
				keywords=[], starargs=None, kwargs=None)
		elif hasattr(func, "k_function_info"):
			args = [func.__self__] + new_args
			inlined, _ = inline(func.k_function_info.k_function, args, dict(), self.rm)
			return inlined
		else:
			args = [ast.Str("rpc"), ast.Num(self.rm.rpc_map[func])]
			args += new_args
			return ast.Call(func=ast.Name("syscall", ast.Load()),
				args=args, keywords=[], starargs=None, kwargs=None)

	def visit_Expr(self, node):
		if isinstance(node.value, ast.Call):
			r = self.visit_Call(node.value)
			if isinstance(r, list):
				return r
			else:
				node.value = r
				return node
		else:
			self.generic_visit(node)
			return node

	def visit_FunctionDef(self, node):
		node.decorator_list = []
		self.generic_visit(node)
		return node

class _ListReadOnlyParams(ast.NodeVisitor):
	def visit_FunctionDef(self, node):
		if hasattr(self, "read_only_params"):
			raise ValueError("More than one function definition")
		self.read_only_params = {arg.arg for arg in node.args.args}
		self.generic_visit(node)

	def visit_Name(self, node):
		if isinstance(node.ctx, ast.Store):
			try:
				self.read_only_params.remove(node.id)
			except KeyError:
				pass

def _list_read_only_params(funcdef):
	lrp = _ListReadOnlyParams()
	lrp.visit(funcdef)
	return lrp.read_only_params

def _initialize_function_params(funcdef, k_args, k_kwargs, rm):
	obj = k_args[0]
	funcname = funcdef.name
	param_init = []
	rop = _list_read_only_params(funcdef)
	for arg_ast, arg_value in zip(funcdef.args.args, k_args):
		arg_name = arg_ast.arg
		if arg_name in rop:
			rm.set(obj, funcname, arg_name, arg_value)
		else:
			target = rm.get(obj, funcname, ast.Name(arg_name, ast.Store()))
			value = _value_to_ast(arg_value)
			param_init.append(ast.Assign(targets=[target], value=value))
	return param_init

def inline(k_function, k_args, k_kwargs, rm=None):
	if rm is None:
		rm = _ReferenceManager()

	funcdef = ast.parse(textwrap.dedent(inspect.getsource(k_function))).body[0]

	param_init = _initialize_function_params(funcdef, k_args, k_kwargs, rm)

	obj = k_args[0]
	funcname = funcdef.name
	rr = _ReferenceReplacer(rm, obj, funcname)
	rr.visit(funcdef)

	funcdef.body[0:0] = param_init

	return funcdef.body, rm.rpc_map
