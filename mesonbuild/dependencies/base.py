# Copyright 2013-2018 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This file contains the detection logic for external dependencies.
# Custom logic for several other packages are in separate files.
import copy
import functools
import os
import re
import itertools
import json
import shlex
import shutil
import textwrap
import typing as T
from enum import Enum
from pathlib import Path, PurePath

from .. import mlog
from .. import mesonlib
from ..compilers import clib_langs
from ..environment import Environment, MachineInfo
from ..mesonlib import MachineChoice, MesonException, OrderedSet, PerMachine
from ..mesonlib import Popen_safe, version_compare_many, version_compare, listify, stringlistify, extract_as_list, split_args
from ..mesonlib import Version, LibType, OptionKey
from ..programs import ExternalProgram, find_external_program
from ..interpreterbase import FeatureDeprecated

if T.TYPE_CHECKING:
    from ..compilers.compilers import Compiler
    DependencyType = T.TypeVar('DependencyType', bound='Dependency')


class DependencyException(MesonException):
    '''Exceptions raised while trying to find dependencies'''


class DependencyMethods(Enum):
    # Auto means to use whatever dependency checking mechanisms in whatever order meson thinks is best.
    AUTO = 'auto'
    PKGCONFIG = 'pkg-config'
    CMAKE = 'cmake'
    # Just specify the standard link arguments, assuming the operating system provides the library.
    SYSTEM = 'system'
    # This is only supported on OSX - search the frameworks directory by name.
    EXTRAFRAMEWORK = 'extraframework'
    # Detect using the sysconfig module.
    SYSCONFIG = 'sysconfig'
    # Specify using a "program"-config style tool
    CONFIG_TOOL = 'config-tool'
    # For backwards compatibility
    SDLCONFIG = 'sdlconfig'
    CUPSCONFIG = 'cups-config'
    PCAPCONFIG = 'pcap-config'
    LIBWMFCONFIG = 'libwmf-config'
    QMAKE = 'qmake'
    # Misc
    DUB = 'dub'


class Dependency:

    @classmethod
    def _process_include_type_kw(cls, kwargs) -> str:
        if 'include_type' not in kwargs:
            return 'preserve'
        if not isinstance(kwargs['include_type'], str):
            raise DependencyException('The include_type kwarg must be a string type')
        if kwargs['include_type'] not in ['preserve', 'system', 'non-system']:
            raise DependencyException("include_type may only be one of ['preserve', 'system', 'non-system']")
        return kwargs['include_type']

    def __init__(self, type_name, kwargs):
        self.name = "null"
        self.version = None  # type: T.Optional[str]
        self.language = None # None means C-like
        self.is_found = False
        self.type_name = type_name
        self.compile_args = []  # type: T.List[str]
        self.link_args = []
        # Raw -L and -l arguments without manual library searching
        # If None, self.link_args will be used
        self.raw_link_args = None
        self.sources = []
        self.methods = process_method_kw(self.get_methods(), kwargs)
        self.include_type = self._process_include_type_kw(kwargs)
        self.ext_deps = []  # type: T.List[Dependency]

    def __repr__(self):
        s = '<{0} {1}: {2}>'
        return s.format(self.__class__.__name__, self.name, self.is_found)

    def is_built(self) -> bool:
        return False

    def summary_value(self) -> T.Union[str, mlog.AnsiDecorator, mlog.AnsiText]:
        if not self.found():
            return mlog.red('NO')
        if not self.version:
            return mlog.green('YES')
        return mlog.AnsiText(mlog.green('YES'), ' ', mlog.cyan(self.version))

    def get_compile_args(self) -> T.List[str]:
        if self.include_type == 'system':
            converted = []
            for i in self.compile_args:
                if i.startswith('-I') or i.startswith('/I'):
                    converted += ['-isystem' + i[2:]]
                else:
                    converted += [i]
            return converted
        if self.include_type == 'non-system':
            converted = []
            for i in self.compile_args:
                if i.startswith('-isystem'):
                    converted += ['-I' + i[8:]]
                else:
                    converted += [i]
            return converted
        return self.compile_args

    def get_all_compile_args(self) -> T.List[str]:
        """Get the compile arguments from this dependency and it's sub dependencies."""
        return list(itertools.chain(self.get_compile_args(),
                                    *[d.get_all_compile_args() for d in self.ext_deps]))

    def get_link_args(self, raw: bool = False) -> T.List[str]:
        if raw and self.raw_link_args is not None:
            return self.raw_link_args
        return self.link_args

    def get_all_link_args(self) -> T.List[str]:
        """Get the link arguments from this dependency and it's sub dependencies."""
        return list(itertools.chain(self.get_link_args(),
                                    *[d.get_all_link_args() for d in self.ext_deps]))

    def found(self) -> bool:
        return self.is_found

    def get_sources(self):
        """Source files that need to be added to the target.
        As an example, gtest-all.cc when using GTest."""
        return self.sources

    @staticmethod
    def get_methods():
        return [DependencyMethods.AUTO]

    def get_name(self):
        return self.name

    def get_version(self) -> str:
        if self.version:
            return self.version
        else:
            return 'unknown'

    def get_include_type(self) -> str:
        return self.include_type

    def get_exe_args(self, compiler):
        return []

    def get_pkgconfig_variable(self, variable_name: str, kwargs: T.Dict[str, T.Any]) -> str:
        raise DependencyException(f'{self.name!r} is not a pkgconfig dependency')

    def get_configtool_variable(self, variable_name):
        raise DependencyException(f'{self.name!r} is not a config-tool dependency')

    def get_partial_dependency(self, *, compile_args: bool = False,
                               link_args: bool = False, links: bool = False,
                               includes: bool = False, sources: bool = False):
        """Create a new dependency that contains part of the parent dependency.

        The following options can be inherited:
            links -- all link_with arguments
            includes -- all include_directory and -I/-isystem calls
            sources -- any source, header, or generated sources
            compile_args -- any compile args
            link_args -- any link args

        Additionally the new dependency will have the version parameter of it's
        parent (if any) and the requested values of any dependencies will be
        added as well.
        """
        raise RuntimeError('Unreachable code in partial_dependency called')

    def _add_sub_dependency(self, deplist: T.Iterable[T.Callable[[], 'Dependency']]) -> bool:
        """Add an internal depdency from a list of possible dependencies.

        This method is intended to make it easier to add additional
        dependencies to another dependency internally.

        Returns true if the dependency was successfully added, false
        otherwise.
        """
        for d in deplist:
            dep = d()
            if dep.is_found:
                self.ext_deps.append(dep)
                return True
        return False

    def get_variable(self, *, cmake: T.Optional[str] = None, pkgconfig: T.Optional[str] = None,
                     configtool: T.Optional[str] = None, internal: T.Optional[str] = None,
                     default_value: T.Optional[str] = None,
                     pkgconfig_define: T.Optional[T.List[str]] = None) -> T.Union[str, T.List[str]]:
        if default_value is not None:
            return default_value
        raise DependencyException(f'No default provided for dependency {self!r}, which is not pkg-config, cmake, or config-tool based.')

    def generate_system_dependency(self, include_type: str) -> T.Type['Dependency']:
        new_dep = copy.deepcopy(self)
        new_dep.include_type = self._process_include_type_kw({'include_type': include_type})
        return new_dep

class InternalDependency(Dependency):
    def __init__(self, version, incdirs, compile_args, link_args, libraries,
                 whole_libraries, sources, ext_deps, variables: T.Dict[str, T.Any]):
        super().__init__('internal', {})
        self.version = version
        self.is_found = True
        self.include_directories = incdirs
        self.compile_args = compile_args
        self.link_args = link_args
        self.libraries = libraries
        self.whole_libraries = whole_libraries
        self.sources = sources
        self.ext_deps = ext_deps
        self.variables = variables

    def __deepcopy__(self, memo: dict) -> 'InternalDependency':
        result = self.__class__.__new__(self.__class__)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k in ['libraries', 'whole_libraries']:
                setattr(result, k, copy.copy(v))
            else:
                setattr(result, k, copy.deepcopy(v, memo))
        return result

    def summary_value(self) -> mlog.AnsiDecorator:
        # Omit the version.  Most of the time it will be just the project
        # version, which is uninteresting in the summary.
        return mlog.green('YES')

    def is_built(self) -> bool:
        if self.sources or self.libraries or self.whole_libraries:
            return True
        return any(d.is_built() for d in self.ext_deps)

    def get_pkgconfig_variable(self, variable_name: str, kwargs: T.Dict[str, T.Any]) -> str:
        raise DependencyException('Method "get_pkgconfig_variable()" is '
                                  'invalid for an internal dependency')

    def get_configtool_variable(self, variable_name):
        raise DependencyException('Method "get_configtool_variable()" is '
                                  'invalid for an internal dependency')

    def get_partial_dependency(self, *, compile_args: bool = False,
                               link_args: bool = False, links: bool = False,
                               includes: bool = False, sources: bool = False):
        final_compile_args = self.compile_args.copy() if compile_args else []
        final_link_args = self.link_args.copy() if link_args else []
        final_libraries = self.libraries.copy() if links else []
        final_whole_libraries = self.whole_libraries.copy() if links else []
        final_sources = self.sources.copy() if sources else []
        final_includes = self.include_directories.copy() if includes else []
        final_deps = [d.get_partial_dependency(
            compile_args=compile_args, link_args=link_args, links=links,
            includes=includes, sources=sources) for d in self.ext_deps]
        return InternalDependency(
            self.version, final_includes, final_compile_args,
            final_link_args, final_libraries, final_whole_libraries,
            final_sources, final_deps, self.variables)

    def get_variable(self, *, cmake: T.Optional[str] = None, pkgconfig: T.Optional[str] = None,
                     configtool: T.Optional[str] = None, internal: T.Optional[str] = None,
                     default_value: T.Optional[str] = None,
                     pkgconfig_define: T.Optional[T.List[str]] = None) -> T.Union[str, T.List[str]]:
        val = self.variables.get(internal, default_value)
        if val is not None:
            return val
        raise DependencyException(f'Could not get an internal variable and no default provided for {self!r}')

    def generate_link_whole_dependency(self) -> T.Type['Dependency']:
        new_dep = copy.deepcopy(self)
        new_dep.whole_libraries += new_dep.libraries
        new_dep.libraries = []
        return new_dep

class HasNativeKwarg:
    def __init__(self, kwargs: T.Dict[str, T.Any]):
        self.for_machine = self.get_for_machine_from_kwargs(kwargs)

    def get_for_machine_from_kwargs(self, kwargs: T.Dict[str, T.Any]) -> MachineChoice:
        return MachineChoice.BUILD if kwargs.get('native', False) else MachineChoice.HOST

class ExternalDependency(Dependency, HasNativeKwarg):
    def __init__(self, type_name, environment: Environment, kwargs, language: T.Optional[str] = None):
        Dependency.__init__(self, type_name, kwargs)
        self.env = environment
        self.name = type_name # default
        self.is_found = False
        self.language = language
        self.version_reqs = kwargs.get('version', None)
        if isinstance(self.version_reqs, str):
            self.version_reqs = [self.version_reqs]
        self.required = kwargs.get('required', True)
        self.silent = kwargs.get('silent', False)
        self.static = kwargs.get('static', False)
        if not isinstance(self.static, bool):
            raise DependencyException('Static keyword must be boolean')
        # Is this dependency to be run on the build platform?
        HasNativeKwarg.__init__(self, kwargs)
        self.clib_compiler = detect_compiler(self.name, environment, self.for_machine, self.language)

    def get_compiler(self):
        return self.clib_compiler

    def get_partial_dependency(self, *, compile_args: bool = False,
                               link_args: bool = False, links: bool = False,
                               includes: bool = False, sources: bool = False):
        new = copy.copy(self)
        if not compile_args:
            new.compile_args = []
        if not link_args:
            new.link_args = []
        if not sources:
            new.sources = []
        if not includes:
            new.include_directories = []
        if not sources:
            new.sources = []

        return new

    def log_details(self):
        return ''

    def log_info(self):
        return ''

    def log_tried(self):
        return ''

    # Check if dependency version meets the requirements
    def _check_version(self):
        if not self.is_found:
            return

        if self.version_reqs:
            # an unknown version can never satisfy any requirement
            if not self.version:
                found_msg = ['Dependency', mlog.bold(self.name), 'found:']
                found_msg += [mlog.red('NO'), 'unknown version, but need:',
                              self.version_reqs]
                mlog.log(*found_msg)

                if self.required:
                    m = 'Unknown version of dependency {!r}, but need {!r}.'
                    raise DependencyException(m.format(self.name, self.version_reqs))

            else:
                (self.is_found, not_found, found) = \
                    version_compare_many(self.version, self.version_reqs)
                if not self.is_found:
                    found_msg = ['Dependency', mlog.bold(self.name), 'found:']
                    found_msg += [mlog.red('NO'),
                                  'found', mlog.normal_cyan(self.version), 'but need:',
                                  mlog.bold(', '.join([f"'{e}'" for e in not_found]))]
                    if found:
                        found_msg += ['; matched:',
                                      ', '.join([f"'{e}'" for e in found])]
                    mlog.log(*found_msg)

                    if self.required:
                        m = 'Invalid version of dependency, need {!r} {!r} found {!r}.'
                        raise DependencyException(m.format(self.name, not_found, self.version))
                    return


class NotFoundDependency(Dependency):
    def __init__(self, environment):
        super().__init__('not-found', {})
        self.env = environment
        self.name = 'not-found'
        self.is_found = False

    def get_partial_dependency(self, *, compile_args: bool = False,
                               link_args: bool = False, links: bool = False,
                               includes: bool = False, sources: bool = False):
        return copy.copy(self)


class ExternalLibrary(ExternalDependency):
    def __init__(self, name, link_args, environment, language, silent=False):
        super().__init__('library', environment, {}, language=language)
        self.name = name
        self.language = language
        self.is_found = False
        if link_args:
            self.is_found = True
            self.link_args = link_args
        if not silent:
            if self.is_found:
                mlog.log('Library', mlog.bold(name), 'found:', mlog.green('YES'))
            else:
                mlog.log('Library', mlog.bold(name), 'found:', mlog.red('NO'))

    def get_link_args(self, language=None, **kwargs):
        '''
        External libraries detected using a compiler must only be used with
        compatible code. For instance, Vala libraries (.vapi files) cannot be
        used with C code, and not all Rust library types can be linked with
        C-like code. Note that C++ libraries *can* be linked with C code with
        a C++ linker (and vice-versa).
        '''
        # Using a vala library in a non-vala target, or a non-vala library in a vala target
        # XXX: This should be extended to other non-C linkers such as Rust
        if (self.language == 'vala' and language != 'vala') or \
           (language == 'vala' and self.language != 'vala'):
            return []
        return super().get_link_args(**kwargs)

    def get_partial_dependency(self, *, compile_args: bool = False,
                               link_args: bool = False, links: bool = False,
                               includes: bool = False, sources: bool = False):
        # External library only has link_args, so ignore the rest of the
        # interface.
        new = copy.copy(self)
        if not link_args:
            new.link_args = []
        return new


def sort_libpaths(libpaths: T.List[str], refpaths: T.List[str]) -> T.List[str]:
    """Sort <libpaths> according to <refpaths>

    It is intended to be used to sort -L flags returned by pkg-config.
    Pkg-config returns flags in random order which cannot be relied on.
    """
    if len(refpaths) == 0:
        return list(libpaths)

    def key_func(libpath):
        common_lengths = []
        for refpath in refpaths:
            try:
                common_path = os.path.commonpath([libpath, refpath])
            except ValueError:
                common_path = ''
            common_lengths.append(len(common_path))
        max_length = max(common_lengths)
        max_index = common_lengths.index(max_length)
        reversed_max_length = len(refpaths[max_index]) - max_length
        return (max_index, reversed_max_length)
    return sorted(libpaths, key=key_func)

def strip_system_libdirs(environment, for_machine: MachineChoice, link_args):
    """Remove -L<system path> arguments.

    leaving these in will break builds where a user has a version of a library
    in the system path, and a different version not in the system path if they
    want to link against the non-system path version.
    """
    exclude = {f'-L{p}' for p in environment.get_compiler_system_dirs(for_machine)}
    return [l for l in link_args if l not in exclude]

def process_method_kw(possible: T.Iterable[DependencyMethods], kwargs) -> T.List[DependencyMethods]:
    method = kwargs.get('method', 'auto')  # type: T.Union[DependencyMethods, str]
    if isinstance(method, DependencyMethods):
        return [method]
    # TODO: try/except?
    if method not in [e.value for e in DependencyMethods]:
        raise DependencyException(f'method {method!r} is invalid')
    method = DependencyMethods(method)

    # This sets per-tool config methods which are deprecated to to the new
    # generic CONFIG_TOOL value.
    if method in [DependencyMethods.SDLCONFIG, DependencyMethods.CUPSCONFIG,
                  DependencyMethods.PCAPCONFIG, DependencyMethods.LIBWMFCONFIG]:
        FeatureDeprecated.single_use(f'Configuration method {method.value}', '0.44', 'Use "config-tool" instead.')
        method = DependencyMethods.CONFIG_TOOL
    if method is DependencyMethods.QMAKE:
        FeatureDeprecated.single_use(f'Configuration method "qmake"', '0.58', 'Use "config-tool" instead.')
        method = DependencyMethods.CONFIG_TOOL

    # Set the detection method. If the method is set to auto, use any available method.
    # If method is set to a specific string, allow only that detection method.
    if method == DependencyMethods.AUTO:
        methods = list(possible)
    elif method in possible:
        methods = [method]
    else:
        raise DependencyException(
            'Unsupported detection method: {}, allowed methods are {}'.format(
                method.value,
                mlog.format_list([x.value for x in [DependencyMethods.AUTO] + list(possible)])))

    return methods

def detect_compiler(name: str, env: Environment, for_machine: MachineChoice,
                    language: T.Optional[str]) -> T.Optional['Compiler']:
    """Given a language and environment find the compiler used."""
    compilers = env.coredata.compilers[for_machine]

    # Set the compiler for this dependency if a language is specified,
    # else try to pick something that looks usable.
    if language:
        if language not in compilers:
            m = name.capitalize() + ' requires a {0} compiler, but ' \
                '{0} is not in the list of project languages'
            raise DependencyException(m.format(language.capitalize()))
        return compilers[language]
    else:
        for lang in clib_langs:
            try:
                return compilers[lang]
            except KeyError:
                continue
    return None
