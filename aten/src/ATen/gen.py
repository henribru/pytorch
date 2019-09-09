import argparse
import os

import yaml
from collections import OrderedDict

import sys
from os import path
sys.path.append(path.dirname(path.abspath(__file__)))

import cwrap_parser
import nn_parse
import native_parse
import preprocess_declarations
import function_wrapper

from code_template import CodeTemplate
from env import BUILD_NAMEDTENSOR


# This file is the top-level entry point for code generation in ATen.
# It takes an arbitrary number of arguments specifying metadata files to
# process (.cwrap, .yaml and .h) and outputs a number generated header
# and cpp files in ATen/ (see invocations of 'write' for each file that
# is written.) It is invoked from cmake; look for the 'cwrap_files'
# variable for an up-to-date list of files which are passed.

parser = argparse.ArgumentParser(description='Generate ATen source files')
parser.add_argument('files', help='cwrap files', nargs='+')

parser.add_argument(
    '-s',
    '--source-path',
    help='path to source directory for ATen',
    default='.')
parser.add_argument(
    '-o',
    '--output-dependencies',
    help='output a list of dependencies into the given file and exit')
parser.add_argument(
    '-d', '--install_dir', help='output directory', default='ATen')
parser.add_argument(
    '--rocm',
    action='store_true',
    help='reinterpret CUDA as ROCm/HIP and adjust filepaths accordingly')
options = parser.parse_args()
gen_to_source = os.environ.get('GEN_TO_SOURCE')  # update source directly as part of gen
if not gen_to_source:
    core_install_dir = os.path.join(options.install_dir, 'core_tmp') if options.install_dir is not None else None
else:
    core_install_dir = os.path.join(options.source_path, 'core')

if options.install_dir is not None and not os.path.exists(options.install_dir):
    os.makedirs(options.install_dir)
if core_install_dir is not None and not os.path.exists(core_install_dir):
    os.makedirs(core_install_dir)


class FileManager(object):
    def __init__(self, install_dir=None):
        self.install_dir = install_dir if install_dir else options.install_dir
        self.filenames = set()
        self.outputs_written = False
        self.undeclared_files = []

    def will_write(self, filename):
        filename = '{}/{}'.format(self.install_dir, filename)
        if self.outputs_written:
            raise Exception("'will_write' can only be called before " +
                            "the call to write_outputs, refactor so outputs are registered " +
                            "before running the generators")
        self.filenames.add(filename)

    def _write_if_changed(self, filename, contents):
        try:
            with open(filename, 'r') as f:
                old_contents = f.read()
        except IOError:
            old_contents = None
        if contents != old_contents:
            with open(filename, 'w') as f:
                f.write(contents)

    def write_outputs(self, filename):
        """Write a file containing the list of all outputs which are
        generated by this script."""
        self._write_if_changed(
            filename,
            ''.join(name + ";" for name in sorted(self.filenames)))
        self.outputs_written = True

    def write(self, filename, s, env=None):
        filename = '{}/{}'.format(self.install_dir, filename)
        if isinstance(s, CodeTemplate):
            assert env is not None
            env['generated_comment'] = "@" + "generated by aten/src/ATen/gen.py"
            s = s.substitute(env)
        self._write_if_changed(filename, s)
        if filename not in self.filenames:
            self.undeclared_files.append(filename)
        else:
            self.filenames.remove(filename)

    def check_all_files_written(self):
        if len(self.undeclared_files) > 0:
            raise Exception(
                "trying to write files {} which are not ".format(self.undeclared_files) +
                "in the list of outputs this script produces. " +
                "use will_write to add them.")
        if len(self.filenames) > 0:
            raise Exception("Outputs declared with 'will_write' were " +
                            "never written: {}".format(self.filenames))


TEMPLATE_PATH = options.source_path + "/templates"
TYPE_DERIVED_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDerived.cpp")
SPARSE_TYPE_DERIVED_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/SparseTypeDerived.cpp")
TYPE_DERIVED_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDerived.h")
TYPE_DEFAULT_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDefault.h")
TYPE_DEFAULT_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/TypeDefault.cpp")
REGISTRATION_DECLARATIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/RegistrationDeclarations.h")
OPS_ALREADY_MOVED_TO_C10_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/OpsAlreadyMovedToC10.cpp")

TENSOR_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorBody.h")
TENSOR_METHODS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/TensorMethods.h")

FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/Functions.h")

LEGACY_TH_FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/LegacyTHFunctions.h")
LEGACY_TH_FUNCTIONS_CPP = CodeTemplate.from_file(TEMPLATE_PATH + "/LegacyTHFunctions.cpp")

NATIVE_FUNCTIONS_H = CodeTemplate.from_file(TEMPLATE_PATH + "/NativeFunctions.h")

core_file_manager = FileManager(core_install_dir)
file_manager = FileManager()
cuda_file_manager = FileManager()

def backend_to_devicetype(backend):
    if backend == 'QuantizedCPU':
        return 'CPU'
    return backend

backends = ['CPU', 'CUDA']
densities = ['Dense', 'Sparse', 'Mkldnn']  # TODO: layout instead of densities?

quantized_backends = ['QuantizedCPU']

# scalar_name, c_type, accreal, is_floating_type
quantized_scalar_types = [
    ('QInt8', 'qint8', 'QInt8AccrealNotDefined', 'QInt8IsFloatingTypeNotDefined'),
    ('QUInt8', 'quint8', 'QUInt8AccrealNotDefined', 'QUInt8IsFloatingTypeNotDefined'),
    ('QInt32', 'qint32', 'QInt32AccrealNotDefined', 'Qint32IsFloatingTypeNotDefined'),
]


# shared environment for non-derived base classes TensorBody.h Storage.h
top_env = {
    'cpu_type_headers': [],
    'cuda_type_headers': [],
    'function_registrations': [],
    'c10_function_registrations': [],
    'c10_ops_already_moved_from_aten_to_c10': [],
    'type_method_declarations': [],
    'type_method_definitions': [],
    'tensor_method_declarations': [],
    'tensor_method_definitions': [],
    'function_declarations': [],
    'function_definitions': [],
    'type_ids': [],
    'native_function_declarations': [],
    'registration_declarations': [],
}


def dict_representer(dumper, data):
    return dumper.represent_dict(data.items())


def postprocess_output_declarations(output_declarations):
    # ensure each return has a name associated with it
    for decl in output_declarations:
        has_named_ret = False
        for n, ret in enumerate(decl.returns):
            if 'name' not in ret:
                assert not has_named_ret
                if decl.inplace:
                    ret['name'] = 'self'
                elif len(decl.returns) == 1:
                    ret['name'] = 'out'
                else:
                    ret['name'] = 'out' + str(n)
            else:
                has_named_ret = True

    def remove_key_if_none(dictionary, key):
        if key in dictionary.keys() and dictionary[key] is None:
            del dictionary[key]
        return dictionary

    return [remove_key_if_none(decl._asdict(), 'buffers')
            for decl in output_declarations]


def format_yaml(data):
    if options.output_dependencies:
        # yaml formatting is slow so don't do it if we will ditch it.
        return ""
    noalias_dumper = yaml.dumper.SafeDumper
    noalias_dumper.ignore_aliases = lambda self, data: True
    # Support serializing OrderedDict
    noalias_dumper.add_representer(OrderedDict, dict_representer)
    # Some yaml parsers (e.g. Haskell's) don't understand line breaks.
    # width=float('Inf') turns off optional line breaks and improves
    # the portability of the outputted yaml.
    return yaml.dump(data, default_flow_style=False, Dumper=noalias_dumper, width=float('Inf'))


def generate_storage_type_and_tensor(backend, density, declarations):
    env = {}
    density_tag = density if density != 'Dense' else ''
    env['Density'] = density
    env['Type'] = "{}{}Type".format(density_tag, backend)
    env['DeviceType'] = backend_to_devicetype(backend)
    env['Backend'] = density_tag + backend
    env['storage_tensor_headers'] = []
    if density != 'Sparse':
        env['storage_tensor_headers'] = ['#include <c10/core/TensorImpl.h>']

    # used for generating switch logic for external functions
    tag = density_tag + backend
    env['TypeID'] = 'TypeID::' + tag
    top_env['type_ids'].append(tag + ',')

    env['legacy_th_headers'] = []
    if backend == 'CUDA':
        env['extra_cuda_headers'] = []
        env['extra_cuda_headers'].append('#include <ATen/DeviceGuard.h>')
        if options.rocm:
            env['th_headers'] = [
                '#include <THH/THH.h>',
                '#include <THH/THHTensor.hpp>',
                '#include <THHUNN/THHUNN.h>',
                '#undef THNN_',
                '#undef THCIndexTensor_',
            ]
            env['extra_cuda_headers'].append('#include <ATen/hip/ATenHIPGeneral.h>')
            env['extra_cuda_headers'].append('#include <ATen/hip/HIPDevice.h>')
            env['extra_cuda_headers'].append('#include <ATen/hip/HIPContext.h>')
        else:
            env['th_headers'] = [
                '#include <THC/THC.h>',
                '#include <THC/THCTensor.hpp>',
                '#include <THCUNN/THCUNN.h>',
                '#undef THNN_',
                '#undef THCIndexTensor_',
            ]
            env['extra_cuda_headers'].append('#include <ATen/cuda/ATenCUDAGeneral.h>')
            env['extra_cuda_headers'].append('#include <ATen/cuda/CUDADevice.h>')
            env['extra_cuda_headers'].append('#include <ATen/cuda/CUDAContext.h>')
        env['state'] = ['globalContext().getTHCState()']
        env['isCUDA'] = 'true'
        env['storage_device'] = 'return storage->device;'
        env['Generator'] = 'CUDAGenerator'
        env['allocator'] = 'at::cuda::getCUDADeviceAllocator()'
    else:
        env['th_headers'] = [
            '#include <TH/TH.h>',
            '#include <TH/THTensor.hpp>',
            '#include <THNN/THNN.h>',
            '#undef THNN_',
        ]
        env['extra_cuda_headers'] = []
        env['state'] = []
        env['isCUDA'] = 'false'
        env['storage_device'] = 'throw std::runtime_error("CPU storage has no device");'
        env['Generator'] = 'CPUGenerator'
        env['allocator'] = 'getCPUAllocator()'

    declarations, definitions, registrations, c10_registrations, th_declarations, th_definitions = function_wrapper.create_derived(
        env, declarations)
    env['type_derived_method_declarations'] = declarations
    env['type_derived_method_definitions'] = definitions
    env['function_registrations'] = registrations
    env['c10_function_registrations'] = c10_registrations
    env['legacy_th_declarations'] = th_declarations
    env['legacy_th_definitions'] = th_definitions

    fm = file_manager
    if env['DeviceType'] == 'CUDA':
        fm = cuda_file_manager

    if env['Backend'] == 'CPU' or env['Backend'] == 'CUDA':
        env['namespace'] = env['Backend'].lower()
        env['legacy_th_headers'].append('#include <ATen/LegacyTHFunctions' + env['Backend'] + ".h>")
        fm.write('LegacyTHFunctions' + env['Backend'] + ".h", LEGACY_TH_FUNCTIONS_H, env)
        fm.write('LegacyTHFunctions' + env['Backend'] + ".cpp", LEGACY_TH_FUNCTIONS_CPP, env)

    if density != 'Sparse':
        fm.write(env['Type'] + ".cpp", TYPE_DERIVED_CPP, env)
    else:
        fm.write(env['Type'] + ".cpp", SPARSE_TYPE_DERIVED_CPP, env)
    fm.write(env['Type'] + ".h", TYPE_DERIVED_H, env)

    if env['DeviceType'] == 'CPU':
        top_env['cpu_type_headers'].append(
            '#include "ATen/{}.h"'.format(env['Type']))
    else:
        assert env['DeviceType'] == 'CUDA'
        top_env['cuda_type_headers'].append(
            '#include "ATen/{}.h"'.format(env['Type']))


# yields (backend, density) tuples
def iterate_types():
    for backend in backends:
        for density in densities:
            if density == 'Mkldnn' and backend != 'CPU':
                continue
            else:
                yield (backend, density)
    for backend in quantized_backends:
        yield (backend, 'Dense')


###################
# declare what files will be output _before_ we do any work
# so that the script runs quickly when we are just querying the
# outputs
def declare_outputs():
    core_files = ['TensorBody.h', 'TensorMethods.h']
    for f in core_files:
        core_file_manager.will_write(f)
    files = ['Declarations.yaml', 'TypeDefault.cpp', 'TypeDefault.h',
             'Functions.h', 'NativeFunctions.h', 'RegistrationDeclarations.h']
    for f in files:
        file_manager.will_write(f)
    for backend, density in iterate_types():
        full_backend = backend if density == "Dense" else density + backend
        fm = file_manager
        if backend == 'CUDA':
            fm = cuda_file_manager
        for kind in ["Type"]:
            if kind != 'Type' and density == "Sparse":
                # No Storage or Tensor for sparse
                continue
            fm.will_write("{}{}.h".format(full_backend, kind))
            fm.will_write("{}{}.cpp".format(full_backend, kind))
        if backend == 'CPU' or backend == 'CUDA':
            fm.will_write("LegacyTHFunctions{}.h".format(backend))
            fm.will_write("LegacyTHFunctions{}.cpp".format(backend))


def filter_by_extension(files, *extensions):
    filtered_files = []
    for file in files:
        for extension in extensions:
            if file.endswith(extension):
                filtered_files.append(file)
    return filtered_files


# because EOL may not be LF(\n) on some environment (e.g. Windows),
# normalize EOL from CRLF/CR to LF and compare both files.
def cmpfiles_with_eol_normalization(a, b, names):
    results = ([], [], [])    # match, mismatch, error
    for x in names:
        try:
            with open(os.path.join(a, x)) as f:
                ax = f.read().replace('\r\n', '\n').replace('\r', '\n')
            with open(os.path.join(b, x)) as f:
                bx = f.read().replace('\r\n', '\n').replace('\r', '\n')
            if ax == bx:
                results[0].append(x)
            else:
                results[1].append(x)
                import difflib
                import sys
                d = difflib.Differ()
                sys.stdout.write('-' * 80 + '\n')
                sys.stdout.write('x={}, a={}, b={}\n'.format(x, a, b))
                for i, line in enumerate(list(d.compare(ax.splitlines(), bx.splitlines()))):
                    if line[:2] != '  ':
                        sys.stdout.write('{:5d}: {}\n'.format(i, line))
                sys.stdout.write('-' * 80 + '\n')
                sys.stdout.write(ax)
                sys.stdout.write('-' * 80 + '\n')
        except OSError:
            results[2].append(x)
    return results


def is_namedtensor_only_decl(decl):
    if 'Dimname' in decl['schema_string']:
        return True
    if decl['name'] == 'align_tensors':
        return True
    return False


def generate_outputs():
    cwrap_files = filter_by_extension(options.files, '.cwrap')
    nn_files = filter_by_extension(options.files, 'nn.yaml', '.h')
    native_files = filter_by_extension(options.files, 'native_functions.yaml')

    declarations = [d
                    for file in cwrap_files
                    for d in cwrap_parser.parse(file)]

    declarations += nn_parse.run(nn_files)
    declarations += native_parse.run(native_files)
    declarations = preprocess_declarations.run(declarations)

    # note: this will fill in top_env['type/tensor_method_declarations/definitions']
    # and modify the declarations to include any information that will all_backends
    # be used by function_wrapper.create_derived
    output_declarations = function_wrapper.create_generic(top_env, declarations)
    output_declarations = postprocess_output_declarations(output_declarations)
    file_manager.write("Declarations.yaml", format_yaml(output_declarations))

    # Filter out named-tensor only declarations.
    # They are necessary in create_generic because that generates Type.h, TensorBody.h,
    # and TensorMethods.h, all of which are checked in to the codebase and therefore
    # need to be consistent whether or not BUILD_NAMEDTENSOR is on/off.
    if not BUILD_NAMEDTENSOR:
        declarations = [decl for decl in declarations
                        if not is_namedtensor_only_decl(decl)]

    for backend, density in iterate_types():
        generate_storage_type_and_tensor(backend, density, declarations)

    core_files = {
        'TensorBody.h': TENSOR_H,
        'TensorMethods.h': TENSOR_METHODS_H,
        'OpsAlreadyMovedToC10.cpp': OPS_ALREADY_MOVED_TO_C10_CPP,
    }

    for core_file, core_template_file in core_files.items():
        core_file_manager.write(core_file, core_template_file, top_env)

    file_manager.write('TypeDefault.h', TYPE_DEFAULT_H, top_env)
    file_manager.write('TypeDefault.cpp', TYPE_DEFAULT_CPP, top_env)
    file_manager.write('RegistrationDeclarations.h', REGISTRATION_DECLARATIONS_H, top_env)

    file_manager.write('Functions.h', FUNCTIONS_H, top_env)

    file_manager.write('NativeFunctions.h', NATIVE_FUNCTIONS_H, top_env)

    file_manager.check_all_files_written()
    cuda_file_manager.check_all_files_written()

    # check that generated files match source files
    core_source_path = os.path.join(options.source_path, 'core')
    match, mismatch, errors = cmpfiles_with_eol_normalization(core_install_dir, core_source_path, core_files.keys())
    if errors:
        raise RuntimeError("Error while trying to compare source and generated files for {}. "
                           "Source directory: {}.  Generated directory: {}."
                           .format(errors, core_source_path, core_install_dir))
    if mismatch:
        file_component = '{}'.format(','.join(mismatch))
        if len(mismatch) > 1:
            file_component = '{' + file_component + '}'
        update_cmd = "cp {}/{} {}".format(core_install_dir, file_component, core_source_path)
        raise RuntimeError("Source files: {} did not match generated files.  To update the source files, "
                           "set environment variable GEN_TO_SOURCE or run \"{}\"".format(mismatch, update_cmd))

declare_outputs()
if options.output_dependencies is not None:
    file_manager.write_outputs(options.output_dependencies)
    core_file_manager.write_outputs(options.output_dependencies + "-core")
    cuda_file_manager.write_outputs(options.output_dependencies + "-cuda")
else:
    generate_outputs()
