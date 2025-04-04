from typing import Optional, Callable, Iterator, Iterable, List, Set, Mapping, Tuple, Any, Dict
from functools import cmp_to_key
import ast
import warnings
import importlib
import itertools
import inspect
import logging
import platform
import sys
import os
import re
from argparse import ArgumentParser

logger = logging.getLogger(__name__)

_visited_objects = []

# A list of function docstring pre-processing hooks
function_docstring_preprocessing_hooks: List[Callable[[str], str]] = []

PYBIND11_STUBGEN_ADD_DLL_DIRECTORY_NAME = "PYBIND11_STUBGEN_ADD_DLL_DIRECTORY"


class DirectoryWalkerGuard(object):

    def __init__(self, dirname):
        self.dirname = dirname

    def __enter__(self):
        if not os.path.exists(self.dirname):
            os.mkdir(self.dirname)

        assert os.path.isdir(self.dirname)

        os.chdir(self.dirname)

    def __exit__(self, exc_type, exc_val, exc_tb):
        os.chdir(os.path.pardir)


_default_pybind11_repr_re = re.compile(r'(<(?P<class>\w+(\.\w+)*) object at 0x[0-9a-fA-F]+>)|'
                                       r'(<(?P<enum>\w+(.\w+)*): \d+>)')


def replace_default_pybind11_repr(line):
    default_reprs = []

    def replacement(m):
        if m.group("class"):
            default_reprs.append(m.group(0))
            return "..."
        return m.group("enum")

    return default_reprs, _default_pybind11_repr_re.sub(replacement, line)


class FunctionSignature(object):
    # When True don't raise an error when invalid signatures/defaultargs are
    # encountered (yes, global variables, blame me)
    ignore_invalid_signature = False
    ignore_invalid_defaultarg = False

    signature_downgrade = True

    # Number of invalid default values found so far
    n_invalid_default_values = 0

    # Number of invalid signatures found so far
    n_invalid_signatures = 0

    @classmethod
    def n_fatal_errors(cls):
        return ((0 if cls.ignore_invalid_defaultarg else cls.n_invalid_default_values) +
                (0 if cls.ignore_invalid_signature else cls.n_invalid_signatures))

    def __init__(self, name, args='*args, **kwargs', rtype='None', validate=True):
        self.name = name
        self.args = args
        self.rtype = rtype

        if validate:
            invalid_defaults, self.args = replace_default_pybind11_repr(self.args)
            if invalid_defaults:
                FunctionSignature.n_invalid_default_values += 1
                lvl = logging.WARNING if FunctionSignature.ignore_invalid_defaultarg else logging.ERROR
                logger.log(lvl, "Default argument value(s) replaced with ellipses (...):")
                for invalid_default in invalid_defaults:
                    logger.log(lvl, "    {}".format(invalid_default))

            if USE_BOOST_PYTHON:
                if args:
                    find_optional_args = re.findall('\[(.*?)\]$', args)
                    optional_args = None
                    if find_optional_args:
                        optional_args = find_optional_args[0]
                    if optional_args:
                        nominal_args = args.replace("[" + optional_args + "]","")
                    else:
                        nominal_args = args

                    num_nominal_args = 0
                    if nominal_args:
                        nominal_args = nominal_args.split(",")
                        num_nominal_args = len(nominal_args)

                    num_optional_args = 0
                    if optional_args:
                        optional_args = optional_args.split("[,")
                        num_optional_args = len(optional_args)
                        if num_optional_args > 1:
                            optional_args[-1] = re.sub(']'*(num_optional_args-1)+'$', '', optional_args[-1]) # Replace at the end
                    new_args = ""

                    if nominal_args:
                        for k,arg in enumerate(nominal_args):
                            type_name = re.findall('\((.*?)\)', arg)[0]
                            arg_name = arg.split(")")[1]
                            arg_name = arg_name.replace(' ','_')

                            new_args += arg_name + ": " + type_name
                            if k < num_nominal_args-1:
                                new_args += ", "

                    if num_optional_args > 0 and num_nominal_args > 0:
                        new_args += ", "

                    if optional_args and True:
                        for k,arg in enumerate(optional_args):
                            # Check for default value
                            split_arg_equal = arg.split('=',maxsplit=1)
                            main_arg = split_arg_equal[0]
                            type_name = re.findall('\((.*?)\)', main_arg)[0]

                            arg_name = main_arg.split(")")[1]
                            arg_name = arg_name.replace(' ','_')
                            new_args += arg_name + ": " + type_name
                            optional_value = None
                            if len(split_arg_equal) > 1:
                                optional_value = split_arg_equal[1]
                                new_args += " = " + optional_value

                            if k < num_optional_args-1:
                                new_args += ", "

                    new_args = new_args.replace(" ,", ",")
                    self.args = new_args
                    args = new_args

                rtype = rtype.split(" :")[0]
                self.rtype = rtype

            function_def_str = "def {sig.name}({sig.args}) -> {sig.rtype}: ...".format(sig=self)
            try:
                ast.parse(function_def_str)
            except SyntaxError as e:
                FunctionSignature.n_invalid_signatures += 1
                if FunctionSignature.signature_downgrade:
                    self.name = name
                    self.args = "*args, **kwargs"
                    self.rtype = "typing.Any"
                    lvl = logging.WARNING if FunctionSignature.ignore_invalid_signature else logging.ERROR
                    logger.log(lvl, "Generated stubs signature is degraded to `(*args, **kwargs) -> typing.Any` for")
                else:
                    lvl = logging.WARNING
                    logger.warning("Ignoring invalid signature:")
                logger.log(lvl, function_def_str)
                logger.log(lvl, " " * (e.offset - 1) + "^-- Invalid syntax")

    def __eq__(self, other):
        return isinstance(other, FunctionSignature) and (self.name, self.args, self.rtype) == (
            other.name, other.args, other.rtype)

    def __hash__(self):
        return hash((self.name, self.args, self.rtype))

    def split_arguments(self):
        if len(self.args.strip()) == 0:
            return []

        prev_stop = 0
        brackets = 0
        splitted_args = []

        for i, c in enumerate(self.args):
            if c == "[":
                brackets += 1
            elif c == "]":
                brackets -= 1
                assert brackets >= 0
            elif c == "," and brackets == 0:
                splitted_args.append(self.args[prev_stop:i])
                prev_stop = i + 1

        splitted_args.append(self.args[prev_stop:])
        assert brackets == 0
        return splitted_args

    @staticmethod
    def argument_type(arg):
        return arg.split(":")[-1].strip()

    def get_all_involved_types(self):
        types = []
        for t in [self.rtype] + self.split_arguments():
            types.extend([m[0] for m in
                          re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*)", self.argument_type(t))
                          ])
        return types


class PropertySignature(object):
    NONE = 0
    READ_ONLY = 1
    WRITE_ONLY = 2
    READ_WRITE = READ_ONLY | WRITE_ONLY

    def __init__(self, rtype, setter_args, access_type):
        self.rtype = rtype
        self.setter_args = setter_args
        self.access_type = access_type

    @property
    def setter_arg_type(self):
        return FunctionSignature.argument_type(FunctionSignature('name', self.setter_args).split_arguments()[1])


# If true numpy.ndarray[int32[3,3]] will be reduced to numpy.ndarray
BARE_NUPMY_NDARRAY = False


def replace_numpy_array(match_obj):
    if BARE_NUPMY_NDARRAY:
        return "numpy.ndarray"
    numpy_type = match_obj.group("type")
    # pybind always append size of data type
    if numpy_type in ['int8', 'int16', 'int32', 'int64',
                      'float16', 'float32', 'float64',
                      'complex32', 'complex64', 'longcomplex'
                      ]:
        numpy_type = "numpy." + numpy_type

    shape = match_obj.group("shape")
    if shape:
        shape = ", _Shape[{}]".format(shape)
    else:
        shape = ""
    result = r"numpy.ndarray[{type}{shape}]".format(type=numpy_type, shape=shape)
    return result


def replace_typing_types(match):
    # pybind used to have iterator/iterable in place of Iterator/Iterable
    return "typing." + match.group('type').capitalize()


# If true, parse BOOST_PYTHON signature
USE_BOOST_PYTHON = False

class StubsGenerator(object):
    INDENT = " " * 4

    GLOBAL_CLASSNAME_REPLACEMENTS = {
        re.compile(
            r"numpy.ndarray\[(?P<type>[^\[\]]+)(\[(?P<shape>[^\[\]]+)\])?(?P<extra>[^][]*)\]"): replace_numpy_array,
        re.compile(
            r"(?<!\w)(?P<type>Callable|Dict|[Ii]terator|[Ii]terable|List|Optional|Set|Tuple|Union')(?!\w)"): replace_typing_types
    }

    def parse(self):
        raise NotImplementedError

    def to_lines(self):  # type: () -> List[str]
        raise NotImplementedError

    @staticmethod
    def _indent(line):  # type: (str) -> str
        return StubsGenerator.INDENT + line

    @staticmethod
    def indent(lines):  # type: (str) -> str
        lines = lines.split("\n")
        lines = [StubsGenerator._indent(l) if l else l for l in lines]
        return "\n".join(lines)

    @staticmethod
    def fully_qualified_name(klass):
        module_name = klass.__module__ if hasattr(klass, '__module__') else None
        class_name = getattr(klass, "__qualname__", klass.__name__)

        if module_name == "builtins":
            return class_name
        else:
            return "{module}.{klass}".format(
                module=module_name,
                klass=class_name)

    @staticmethod
    def apply_classname_replacements(s):  # type: (str) -> Any
        for k, v in StubsGenerator.GLOBAL_CLASSNAME_REPLACEMENTS.items():
            s = k.sub(v, s)
        return s

    @staticmethod
    def function_signatures_from_docstring(name, func, module_name):  # type: (Any, str) -> List[FunctionSignature]
        try:
            no_parentheses = r"[^()]*"
            parentheses_one_fold = r"({nopar}(\({nopar}\))?)*".format(nopar=no_parentheses)
            parentheses_two_fold = r"({nopar}(\({par1}\))?)*".format(par1=parentheses_one_fold, nopar=no_parentheses)
            parentheses_three_fold = r"({nopar}(\({par2}\))?)*".format(par2=parentheses_two_fold, nopar=no_parentheses)
            signature_regex = r"(\s*(?P<overload_number>\d+).)" \
                              r"?\s*{name}\s*\((?P<args>{balanced_parentheses})\)" \
                              r"\s*->\s*" \
                              r"(?P<rtype>[^\(\)]+)\s*".format(name=name,
                                                               balanced_parentheses=parentheses_three_fold)
            docstring = func.__doc__

            for hook in function_docstring_preprocessing_hooks:
                docstring = hook(docstring)

            signatures = []
            for line in docstring.split("\n"):
                m = re.match(signature_regex, line)
                if m:
                    args = m.group("args")
                    rtype = m.group("rtype")
                    signatures.append(FunctionSignature(name, args, rtype))

            # strip module name if provided
            if module_name:
                for sig in signatures:
                    regex = r"{}\.(\w+)".format(module_name.replace(".", r"\."))
                    sig.args = re.sub(regex, r"\g<1>", sig.args)
                    sig.rtype = re.sub(regex, r"\g<1>", sig.rtype)

            for sig in signatures:
                sig.args = StubsGenerator.apply_classname_replacements(sig.args)
                sig.rtype = StubsGenerator.apply_classname_replacements(sig.rtype)

            return sorted(list(set(signatures)),
                          key=lambda fs: fs.args)
        except AttributeError:
            return []

    @staticmethod
    def property_signature_from_docstring(prop, module_name):  # type:  (Any, str)-> PropertySignature

        getter_rtype = "None"
        setter_args = "None"
        access_type = PropertySignature.NONE

        strip_module_name = module_name is not None

        if hasattr(prop, "fget") and prop.fget is not None:
            access_type |= PropertySignature.READ_ONLY
            if hasattr(prop.fget, "__doc__") and prop.fget.__doc__ is not None:
                for line in prop.fget.__doc__.split("\n"):
                    if strip_module_name:
                        line = line.replace(module_name + ".", "")
                    m = re.match(r"\s*(\w*)\((?P<args>[^()]*)\)\s*->\s*(?P<rtype>[^()]+)\s*", line)
                    if m:
                        getter_rtype = m.group("rtype")
                        break

        if hasattr(prop, "fset") and prop.fset is not None:
            access_type |= PropertySignature.WRITE_ONLY
            if hasattr(prop.fset, "__doc__") and prop.fset.__doc__ is not None:
                for line in prop.fset.__doc__.split("\n"):
                    if strip_module_name:
                        line = line.replace(module_name + ".", "")
                    m = re.match(r"\s*(\w*)\((?P<args>[^()]*)\)\s*->\s*(?P<rtype>[^()]+)\s*", line)
                    if m:
                        args = m.group("args")
                        # replace first argument with self
                        setter_args = ",".join(["self"] + args.split(",")[1:])
                        break
        getter_rtype = StubsGenerator.apply_classname_replacements(getter_rtype)
        setter_args = StubsGenerator.apply_classname_replacements(setter_args)
        return PropertySignature(getter_rtype, setter_args, access_type)

    @staticmethod
    def remove_signatures(docstring):  # type: (str) ->str

        if docstring is None:
            return ""

        for hook in function_docstring_preprocessing_hooks:
            docstring = hook(docstring)

        signature_regex = r"(\s*(?P<overload_number>\d+).\s*)" \
                          r"?{name}\s*\((?P<args>.*)\)\s*(->\s*(?P<rtype>[^\(\)]+)\s*)?".format(name=r"\w+")

        lines = docstring.split("\n\n")
        lines = filter(lambda line: line != "Overloaded function.", lines)

        return "\n\n".join(filter(lambda line: not re.match(signature_regex, line), lines))

    @staticmethod
    def sanitize_docstring(docstring):  # type: (str) ->str
        docstring = StubsGenerator.remove_signatures(docstring)
        docstring = docstring.rstrip("\n")

        if docstring and re.match(r"^\s*$", docstring):
            docstring = ""

        return docstring

    @staticmethod
    def format_docstring(docstring):
        return StubsGenerator.indent('"""\n{}\n"""'.format(docstring.strip("\n")))


class AttributeStubsGenerator(StubsGenerator):
    def __init__(self, name, attribute):  # type: (str, Any)-> None
        self.name = name
        self.attr = attribute

    def parse(self):
        if self in _visited_objects:
            return
        _visited_objects.append(self)

    def is_safe_to_use_repr(self, value):
        if value is None or isinstance(value, (int, str)):
            return True
        if isinstance(value, (float, complex)):
            try:
                eval(repr(value))
                return True
            except (SyntaxError, NameError):
                return False
        if isinstance(value, (list, tuple, set)):
            for x in value:
                if not self.is_safe_to_use_repr(x):
                    return False
            return True
        if isinstance(value, dict):
            for k, v in value.items():
                if not self.is_safe_to_use_repr(k) or not self.is_safe_to_use_repr(v):
                    return False
            return True
        return False

    def to_lines(self):  # type: () -> List[str]
        if self.is_safe_to_use_repr(self.attr):
            return [
                "{name} = {repr}".format(
                    name=self.name,
                    repr=repr(self.attr)
                )
            ]

        # special case for modules
        # https://github.com/sizmailov/pybind11-stubgen/issues/43
        if type(self.attr) is type(os) and hasattr(self.attr, "__name__"):
            return [
                "{name} = {repr}".format(
                    name=self.name,
                    repr=self.attr.__name__
                )
            ]

        value_lines = repr(self.attr).split("\n")
        if len(value_lines) == 1:
            value = value_lines[0]
            # remove random address from <foo.Foo object at 0x1234>
            value = re.sub(r" at 0x[0-9a-fA-F]+>", ">", value)
            typename = self.fully_qualified_name(type(self.attr))
            if value == "<{typename} object>".format(typename=typename):
                value_comment = ""
            else:
                value_comment = " # value = {value}".format(value=value)
            return [
                "{name}: {typename}{value_comment}".format(
                    name=self.name,
                    typename=typename,
                    value_comment=value_comment)
            ]
        else:
            return [
                       "{name}: {typename} # value = ".format(
                           name=self.name,
                           typename=str(type(self.attr)))
                   ] \
                   + ['"""'] \
                   + [l.replace('"""', r'\"\"\"') for l in value_lines] \
                   + ['"""']

    def get_involved_modules_names(self):  # type: () -> Set[str]
        if type(self.attr) is type(os):
            return {self.attr.__name__}
        return {self.attr.__class__.__module__}


class FreeFunctionStubsGenerator(StubsGenerator):
    def __init__(self, name, free_function, module_name):
        self.name = name
        self.member = free_function
        self.module_name = module_name
        self.signatures = []  # type:  List[FunctionSignature]

    def parse(self):
        self.signatures = self.function_signatures_from_docstring(self.name, self.member, self.module_name)

    def to_lines(self):  # type: () -> List[str]
        result = []
        docstring = self.sanitize_docstring(self.member.__doc__)
        if not docstring and not (self.name.startswith("__") and self.name.endswith("__")):
            logger.debug("Docstring is empty for '%s'" % self.fully_qualified_name(self.member))
        for sig in self.signatures:
            if len(self.signatures) > 1:
                result.append("@typing.overload")
            result.append("def {name}({args}) -> {rtype}:".format(
                name=sig.name,
                args=sig.args,
                rtype=sig.rtype
            ))
            if docstring:
                result.append(self.format_docstring(docstring))
                docstring = None  # don't print docstring for other overloads
            else:
                result.append(self.indent("pass"))

        return result

    def get_involved_modules_names(self):  # type: () -> Set[str]
        involved_modules_names = set()
        for s in self.signatures:  # type: FunctionSignature
            for t in s.get_all_involved_types():  # type: str
                try:
                    i = t.rindex(".")
                    involved_modules_names.add(t[:i])
                except ValueError:
                    pass
        return involved_modules_names


class ClassMemberStubsGenerator(FreeFunctionStubsGenerator):
    def __init__(self, name, free_function, module_name):
        super(ClassMemberStubsGenerator, self).__init__(name, free_function, module_name)

    def to_lines(self):  # type: () -> List[str]
        result = []
        docstring = self.sanitize_docstring(self.member.__doc__)
        if not docstring and not (self.name.startswith("__") and self.name.endswith("__")):
            logger.debug("Docstring is empty for '%s'" % self.fully_qualified_name(self.member))
        for sig in self.signatures:
            args = sig.args
            if not args.strip().startswith("self"):
                result.append("@staticmethod")
            else:
                # remove type of self
                args = ",".join(["self"] + sig.split_arguments()[1:])
            if len(self.signatures) > 1:
                result.append("@typing.overload")

            result.append("def {name}({args}) -> {rtype}: {ellipsis}".format(
                name=sig.name,
                args=args,
                rtype=sig.rtype,
                ellipsis="" if docstring else "..."
            ))
            if docstring:
                result.append(self.format_docstring(docstring))
                docstring = None  # don't print docstring for other overloads
        return result


class PropertyStubsGenerator(StubsGenerator):
    def __init__(self, name, prop, module_name):
        self.name = name
        self.prop = prop
        self.module_name = module_name
        self.signature = None  # type: PropertySignature

    def parse(self):
        self.signature = self.property_signature_from_docstring(self.prop, self.module_name)

    def to_lines(self):  # type: () -> List[str]

        docstring = self.sanitize_docstring(self.prop.__doc__)
        rtype = self.signature.rtype
        if rtype == "None":
            match = re.match(r"(.*)\[returns (.+)\](.*)", docstring)
            if match:
                rtype = match.group(2)
        docstring_prop = "\n\n".join([docstring, ":type: {rtype}".format(rtype=rtype)])

        result = ["@property",
                  "def {field_name}(self) -> {rtype}:".format(field_name=self.name, rtype=rtype),
                  self.format_docstring(docstring_prop)]

        if self.signature.setter_args != "None":
            result.append("@{field_name}.setter".format(field_name=self.name))
            result.append(
                "def {field_name}({args}) -> {rtype}:".format(field_name=self.name, args=self.signature.setter_args), rtype=rtype)
            if docstring:
                result.append(self.format_docstring(docstring))
            else:
                result.append(self.indent("pass"))

        return result


class ClassStubsGenerator(StubsGenerator):
    ATTRIBUTES_BLACKLIST = ("__class__", "__module__", "__qualname__", "__dict__", "__weakref__", "__annotations__")
    PYBIND11_ATTRIBUTES_BLACKLIST = ("__entries",)
    METHODS_BLACKLIST = ("__dir__", "__sizeof__")
    BASE_CLASS_BLACKLIST = ("pybind11_object", "object")
    CLASS_NAME_BLACKLIST = ("pybind11_type",)

    def __init__(self,
                 klass,
                 attributes_blacklist=ATTRIBUTES_BLACKLIST,
                 pybind11_attributes_blacklist=PYBIND11_ATTRIBUTES_BLACKLIST,
                 base_class_blacklist=BASE_CLASS_BLACKLIST,
                 methods_blacklist=METHODS_BLACKLIST,
                 class_name_blacklist=CLASS_NAME_BLACKLIST
                 ):
        self.klass = klass
        assert inspect.isclass(klass)

        self.doc_string = None  # type: Optional[str]

        self.classes = []  # type: List[ClassStubsGenerator]
        self.fields = []  # type: List[AttributeStubsGenerator]
        self.properties = []  # type: List[PropertyStubsGenerator]
        self.methods = []  # type: List[ClassMemberStubsGenerator]

        self.base_classes = []
        self.involved_modules_names = set()  # Set[str]

        self.attributes_blacklist = attributes_blacklist
        self.pybind11_attributes_blacklist = pybind11_attributes_blacklist
        self.base_class_blacklist = base_class_blacklist
        self.methods_blacklist = methods_blacklist
        self.class_name_blacklist = class_name_blacklist

    def get_involved_modules_names(self):
        return self.involved_modules_names

    def parse(self):
        if self.klass in _visited_objects:
            return
        _visited_objects.append(self.klass)

        bases = inspect.getmro(self.klass)[1:]

        def is_base_member(name, member):
            for base in bases:
                if hasattr(base, name) and getattr(base, name) is member:
                    return True
            return False

        is_pybind11 = any(base.__name__ == 'pybind11_object' for base in bases)

        for name, member in inspect.getmembers(self.klass):
            # check if attribute is in __dict__ (fast path) before slower search in base classes
            if name not in self.klass.__dict__ and is_base_member(name, member):
                continue
            if name.startswith('__pybind11_module'):
                continue
            if inspect.isroutine(member):
                self.methods.append(ClassMemberStubsGenerator(name, member, self.klass.__module__))
            elif name != '__class__' and inspect.isclass(member):
                if member.__name__ not in self.class_name_blacklist:
                    self.classes.append(ClassStubsGenerator(member))
            elif isinstance(member, property):
                self.properties.append(PropertyStubsGenerator(name, member, self.klass.__module__))
            elif name == "__doc__":
                self.doc_string = member
            elif not (name in self.attributes_blacklist or
                      (is_pybind11 and name in self.pybind11_attributes_blacklist)):
                self.fields.append(AttributeStubsGenerator(name, member))
                # logger.warning("Unknown member %s type : `%s` " % (name, str(type(member))))

        for x in itertools.chain(self.classes,
                                 self.methods,
                                 self.properties,
                                 self.fields):
            x.parse()

        for B in bases:
            if B.__name__ != self.klass.__name__ and B.__name__ not in self.base_class_blacklist:
                self.base_classes.append(B)
                self.involved_modules_names.add(B.__module__)

        for f in self.methods:  # type: ClassMemberStubsGenerator
            self.involved_modules_names |= f.get_involved_modules_names()

        for attr in self.fields:
            self.involved_modules_names |= attr.get_involved_modules_names()

    def to_lines(self):  # type: () -> List[str]

        def strip_current_module_name(obj, module_name):
            regex = r"{}\.(\w+)".format(module_name.replace(".", r"\."))
            return re.sub(regex, r"\g<1>", obj)

        base_classes_list = [
            strip_current_module_name(self.fully_qualified_name(b), self.klass.__module__)
            for b in self.base_classes
        ]
        result = [
            "class {class_name}({base_classes_list}):{doc_string}".format(
                class_name=self.klass.__name__,
                base_classes_list=", ".join(base_classes_list),
                doc_string='\n' + self.format_docstring(self.doc_string)
                if self.doc_string else "",
            ),
        ]
        for cl in self.classes:
            result.extend(map(self.indent, cl.to_lines()))

        for f in self.methods:
            if f.name not in self.methods_blacklist:
                result.extend(map(self.indent, f.to_lines()))

        for p in self.properties:
            result.extend(map(self.indent, p.to_lines()))

        for p in self.fields:
            result.extend(map(self.indent, p.to_lines()))

        result.append(self.indent("pass"))
        return result


class ModuleStubsGenerator(StubsGenerator):
    CLASS_NAME_BLACKLIST = ClassStubsGenerator.CLASS_NAME_BLACKLIST
    ATTRIBUTES_BLACKLIST = ("__file__", "__loader__", "__name__", "__package__",
                            "__spec__", "__path__", "__cached__", "__builtins__")

    def __init__(self, module_or_module_name,
                 attributes_blacklist=ATTRIBUTES_BLACKLIST,
                 class_name_blacklist=CLASS_NAME_BLACKLIST
                 ):
        if isinstance(module_or_module_name, str):
            self.module = importlib.import_module(module_or_module_name)
        else:
            self.module = module_or_module_name
            assert inspect.ismodule(self.module)

        self.doc_string = None  # type: Optional[str]
        self.classes = []  # type: List[ClassStubsGenerator]
        self.free_functions = []  # type: List[FreeFunctionStubsGenerator]
        self.submodules = []  # type: List[ModuleStubsGenerator]
        self.imported_modules = []  # type: List[str]
        self.imported_classes = {}  # type: Dict[str, type]
        self.attributes = []  # type: List[AttributeStubsGenerator]
        self.stub_suffix = ""
        self.write_setup_py = False

        self.attributes_blacklist = attributes_blacklist
        self.class_name_blacklist = class_name_blacklist

    def parse(self):
        if self.module in _visited_objects:
            return
        _visited_objects.append(self.module)
        logger.debug("Parsing '%s' module" % self.module.__name__)
        for name, member in inspect.getmembers(self.module):
            if inspect.ismodule(member):
                m = ModuleStubsGenerator(member)
                if m.module.__name__.split('.')[:-1] == self.module.__name__.split('.'):
                    self.submodules.append(m)
                else:
                    self.imported_modules += [m.module.__name__]
                    logger.debug("Skip '%s' module while parsing '%s' " % (m.module.__name__, self.module.__name__))
            elif inspect.isbuiltin(member) or inspect.isfunction(member):
                self.free_functions.append(FreeFunctionStubsGenerator(name, member, self.module.__name__))
            elif type(member) is type:
                logger.debug("Skip '%s' type while parsing '%s' " % (name, self.module.__name__))
                pass
            elif inspect.isclass(member):
                if member.__module__ == self.module.__name__:
                    if member.__name__ not in self.class_name_blacklist:
                        self.classes.append(ClassStubsGenerator(member))
                else:
                    self.imported_classes[name] = member
                    importlib.import_module(member.__module__)
                    self.classes.append(ClassStubsGenerator(member))
                    self.classes[-1].parse()
            elif name == "__doc__":
                self.doc_string = member
            elif name not in self.attributes_blacklist:
                self.attributes.append(AttributeStubsGenerator(name, member))

        for x in itertools.chain(self.submodules,
                                 self.classes,
                                 self.free_functions,
                                 self.attributes):
            x.parse()

        def class_ordering(a, b):  # type: (ClassStubsGenerator, ClassStubsGenerator) -> int
            if a.klass is b.klass:
                return 0
            if issubclass(a.klass, b.klass):
                return -1
            if issubclass(b.klass, a.klass):
                return 1
            return 0

        # reorder classes so base classes would be printed before derived
        # print([ k.klass.__name__ for k in self.classes ])
        for i in range(len(self.classes)):
            for j in range(i + 1, len(self.classes)):
                if class_ordering(self.classes[i], self.classes[j]) < 0:
                    t = self.classes[i]
                    self.classes[i] = self.classes[j]
                    self.classes[j] = t
        # print( [ k.klass.__name__ for k in self.classes ] )

    def get_involved_modules_names(self):
        result = set(self.imported_modules)

        for attr in self.attributes:
            result |= attr.get_involved_modules_names()

        for C in self.classes:  # type: ClassStubsGenerator
            result |= C.get_involved_modules_names()

        for f in self.free_functions:  # type: FreeFunctionStubsGenerator
            result |= f.get_involved_modules_names()

        return set(result) - {"builtins", 'typing', self.module.__name__}

    def to_lines(self):  # type: () -> List[str]

        result = []

        if self.doc_string:
            result += ['"""' + self.doc_string.replace('"""', r'\"\"\"') + '"""']

        result += [
            "import {}".format(self.module.__name__)
        ]

        # import everything from typing
        result += [
            "import typing"
        ]

        globals_ = {}
        exec("from {} import *".format(self.module.__name__), globals_)

        result += [""]
        all_ = set(globals_.keys()) - {"__builtins__"}
        result.append("__all__ = [\n    " + ",\n    ".join(map(lambda s: '"%s"' % s, sorted(all_))) + "\n]\n")

        for x in itertools.chain(self.classes,
                                 self.free_functions):
            result.extend(x.to_lines())
            result += [""]

        for x in itertools.chain(self.attributes):
            result.extend(x.to_lines())

        # import used packages
        used_modules = sorted(self.get_involved_modules_names())
        if used_modules:
            # result.append("if TYPE_CHECKING:")
            # result.extend(map(self.indent, map(lambda m: "import {}".format(m), used_modules)))
            result.extend(map(lambda mod: "import {}".format(mod), used_modules))

        if "numpy" in used_modules and not BARE_NUPMY_NDARRAY:
            result += [
                "_Shape = typing.Tuple[int, ...]"
            ]

        # add space between imports and rest of module
        result += [""]


        result.append("")  # Newline at EOF
        return result

    @property
    def short_name(self):
        return self.module.__name__.split(".")[-1]

    def write(self):
        if not os.path.exists(self.short_name + self.stub_suffix):
            logger.debug("mkdir `%s`" % (self.short_name + self.stub_suffix))
            os.mkdir(self.short_name + self.stub_suffix)

        with DirectoryWalkerGuard(self.short_name + self.stub_suffix):
            with open("__init__.pyi", "w", encoding="utf-8") as init_pyi:
                init_pyi.write("\n".join(self.to_lines()))
            for m in self.submodules:
                m.write()

            if self.write_setup_py:
                with open("setup.py", "w") as setuppy:
                    setuppy.write("""from setuptools import setup
import os


def find_stubs(package):
    stubs = []
    for root, dirs, files in os.walk(package):
        for file in files:
            path = os.path.join(root, file).replace(package + os.sep, '', 1)
            stubs.append(path)
    return dict(package=stubs)


setup(
    name='{package_name}-stubs',
    maintainer="{package_name} Developers",
    maintainer_email="example@python.org",
    description="PEP 561 type stubs for {package_name}",
    version='1.0',
    packages=['{package_name}-stubs'],
    # PEP 561 requires these
    install_requires=['{package_name}'],
    package_data=find_stubs('{package_name}-stubs'),
)""".format(package_name=self.short_name))


def recursive_mkdir_walker(subdirs, callback):  # type: (List[str], Callable) -> None
    if len(subdirs) == 0:
        callback()
    else:
        if not os.path.exists(subdirs[0]):
            os.mkdir(subdirs[0])
        with DirectoryWalkerGuard(subdirs[0]):
            recursive_mkdir_walker(subdirs[1:], callback)

def main(args=None):
    parser = ArgumentParser(prog='pybind11-stubgen', description="Generates stubs for specified modules")
    parser.add_argument("-o", "--output-dir", help="the root directory for output stubs", default="./stubs")
    parser.add_argument("--root-module-suffix", type=str, default="-stubs", dest='root_module_suffix',
                        help="optional suffix to disambiguate from the original package")
    parser.add_argument("--root_module_suffix", type=str, default=None, dest='root_module_suffix_deprecated',
                        help="Deprecated.  Use `--root-module-suffix`")
    parser.add_argument("--no-setup-py", action='store_true')
    parser.add_argument("--non-stop", action='store_true', help="Deprecated. Use `--ignore-invalid=all`")
    parser.add_argument("--ignore-invalid", nargs="+", choices=["signature", "defaultarg", "all"], default=[],
                        help="Ignore invalid specified python expressions in docstrings")
    parser.add_argument("--skip-signature-downgrade", action='store_true',
                        help="Do not downgrade invalid function signatures to func(*args, **kwargs)")
    parser.add_argument("--bare-numpy-ndarray", action='store_true', default=False,
                        help="Render `numpy.ndarray` without (non-standardized) bracket-enclosed type and shape info")
    parser.add_argument("module_names", nargs="+", metavar="MODULE_NAME", type=str, help="modules names")
    parser.add_argument("--log-level", default="INFO", help="Set output log level")
    parser.add_argument("--boost-python", action="store_true")

    sys_args = parser.parse_args(args or sys.argv[1:])

    if sys_args.non_stop:
        sys_args.ignore_invalid = ['all']
        warnings.warn("`--non-stop` is deprecated in favor of `--ignore-invalid=all`", FutureWarning)

    if sys_args.bare_numpy_ndarray:
        global BARE_NUPMY_NDARRAY
        BARE_NUPMY_NDARRAY = True

    if sys_args.boost_python:
        global USE_BOOST_PYTHON
        USE_BOOST_PYTHON = True

    if 'all' in sys_args.ignore_invalid:
        FunctionSignature.ignore_invalid_signature = True
        FunctionSignature.ignore_invalid_defaultarg = True
    else:
        if 'signature' in sys_args.ignore_invalid:
            FunctionSignature.ignore_invalid_signature = True
        if 'defaultarg' in sys_args.ignore_invalid:
            FunctionSignature.ignore_invalid_defaultarg = True

    if sys_args.skip_signature_downgrade:
        FunctionSignature.signature_downgrade = False

    if sys_args.root_module_suffix_deprecated is not None:
        sys_args.root_module_suffix = sys_args.root_module_suffix_deprecated
        warnings.warn("`--root_module_suffix` is deprecated in favor of `--root-module-suffix`", FutureWarning)

    # On Windows with Python 3.8+, Python doesn't search DLL in PATH anymore
    # We must specify DLL search path manually with `os.add_dll_directory`
    # See https://github.com/python/cpython/issues/87339#issuecomment-1093902060
    if platform.system() == "Windows":
        dll_directories_str = os.getenv(PYBIND11_STUBGEN_ADD_DLL_DIRECTORY_NAME, "")
        dll_directories = map(lambda x: x.strip(), dll_directories_str.split(";"))
        dll_directories = filter(lambda x: len(x) > 0, dll_directories)
        for dll_dir in dll_directories:
            logger.debug(f"Add {dll_dir} to the DLL search path")
            os.add_dll_directory(dll_dir)

    stderr_handler = logging.StreamHandler(sys.stderr)
    handlers = [stderr_handler]

    logging.basicConfig(
        level=logging.getLevelName(sys_args.log_level),
        format='[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s',
        handlers=handlers
    )

    output_path = sys_args.output_dir

    if not os.path.exists(output_path):
        os.mkdir(output_path)

    with DirectoryWalkerGuard(output_path):
        for _module_name in sys_args.module_names:
            _module = ModuleStubsGenerator(_module_name)
            _module.parse()
            if FunctionSignature.n_fatal_errors() == 0:
                _module.stub_suffix = sys_args.root_module_suffix
                _module.write_setup_py = not sys_args.no_setup_py
                recursive_mkdir_walker(_module_name.split(".")[:-1], lambda: _module.write())

        if FunctionSignature.n_invalid_signatures > 0:
            logger.info("Useful link: Avoiding C++ types in docstrings:")
            logger.info("      https://pybind11.readthedocs.io/en/latest/advanced/misc.html"
                        "#avoiding-cpp-types-in-docstrings")

        if FunctionSignature.n_invalid_default_values > 0:
            logger.info("Useful link: Default argument representation:")
            logger.info("      https://pybind11.readthedocs.io/en/latest/advanced/functions.html"
                        "#default-arguments-revisited")

        if FunctionSignature.n_fatal_errors() > 0:
            exit(1)


if __name__ == "__main__":
    main()
