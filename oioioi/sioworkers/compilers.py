from sio.compilers.system_gcc import CStyleCompiler


class CPP23Compiler(CStyleCompiler):
    lang = "cpp"
    compiler = "g++-14"
    options = ["-std=c++23", "-O2", "-s", "-lm"]


def run_cpp_gcc14_2_cpp23_amd64(environ):
    return CPP23Compiler().compile(environ)
