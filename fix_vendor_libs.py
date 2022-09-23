#!/usr/bin/env python3

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

# Checks / fixes every lib in a vendor-PLATFORM.tar.gz archive according to the following rules.
# This includes the libraries that are currently embedded inside wheels.
#
# - fix_names(work_dir, **kwargs)
#   All libraries must have install-names that are simply their own filename - not any other kind of path - or no install name at all.
#
# - fix_rpaths:
#   (Note that @loader_path on Darwin and $ORIGIN on Linux expand to the directory of the binary or shared object doing
#   the loading - they are both referred to as LOADER_PATH for convenience.)
#   All libraries must have an RPATH of LOADER_PATH to ensure they can find deps in the same folder.
#   All libraries that are not / will not be in env/lib must also have an RPATH of LOADER_PATH/<path-to-env-lib>,
#   using the library's eventual install location for libraries that are currently inside wheels.
#
# - fix_deps:
#   All deps must be contained in the archive unless they are on the UNSATISFIED_DEPS_ALLOW_LIST.
#   All deps must be named for the actual library that they need to find, not a symlink to it.
#   All deps from one library inside the archive to another library inside the archive must be specified in the following way:
#     * On Darwin: @rpath/<name-of-library>
#     * On Linux: simply <name-of-library>
#   Deps to libraries not contained in the archive are left unchanged.


USAGE = """

Usage: fix_vendor_libs INPUT_PATH [OUTPUT_PATH]")
    INPUT_PATH is the path to a vendor archive (eg vendor-Darwin.tar.gz),
        or a path to the uncompressed contents of a vendor archive.
    OUTPUT_PATH the path to which the fixed vendor archive is written.
        If not supplied, fix_vendor_libs runs in a dry-run mode where it fixes
        the archive in a temp directory, but doesn't output it anywhere.
"""

SITE_PACKAGES_PREFIX = "env/lib/python3.x/site-packages/"

PLATFORM = platform.system()

if PLATFORM == "Darwin":
    VENDOR_ARCHIVE_NAME = "vendor-Darwin.tar.gz"
    LOADER_PATH = "@loader_path"
    RPATH_PREFIX = "@rpath/"
    LIB_EXTENSIONS = [".dylib", ".so"]
elif PLATFORM == "Linux":
    VENDOR_ARCHIVE_NAME = "vendor-Linux.tar.gz"
    LOADER_PATH = "$ORIGIN"
    RPATH_PREFIX = ""
    LIB_EXTENSIONS = [".so", ".so.*"]


if PLATFORM == "Darwin":
    UNSATISFIED_DEPS_ALLOW_LIST = [
        "libSystem.B.dylib",
        "libbrotlicommon.1.dylib",
        "libc++.1.dylib",
        "libiconv.2.dylib",
        "libresolv.9.dylib",
        "libsasl2.2.dylib",
        "libz.1.dylib",
    ]
elif PLATFORM == "Linux":
    UNSATISFIED_DEPS_ALLOW_LIST = [
        "ld-linux-x86-64.so.2",
        "libc.so.6",
        "libcrypto.so.10",
        "libdl.so.2",
        "libexpat.so.1",
        "libgcc_s.so.1",
        "libm.so.6",
        "libodbc.so.2",
        "libpcre.so.1",
        "libpcreposix.so.0",
        "libpthread.so.0",
        "libpython3.7m.so.1.0",
        "libresolv.so.2",
        "librt.so.1",
        "libssl.so.10",
        "libstdc++.so.6",
        "libz.so.1",
    ]


UNSATISFIED_DEPS_ALLOW_SET = set(UNSATISFIED_DEPS_ALLOW_LIST)


class PlatformSpecific:
    """Marker for functions that vary by platform."""


def info(message):
    print(message)


def checkmark(message):
    print(f"✅  {message}")


def warn(message, make_fatal=False, detail=None):
    if detail:
        message = "\n".join([message, detail])
    if make_fatal:
        fatal(message)
    else:
        print(f"⚠️  {message}", file=sys.stderr)


def fatal(message):
    print(f"❌  {message}", file=sys.stderr)
    sys.exit(1)


def json_dumps(json_obj, root_path):
    def default(unhandled):
        if isinstance(unhandled, Path):
            return os.path.relpath(unhandled, root_path)
        raise TypeError

    return json.dumps(json_obj, indent=2, default=default)


def unpack_all(input_path, work_dir):
    contents_path = work_dir / f"{VENDOR_ARCHIVE_NAME}-contents"
    if input_path.is_file():
        info(f"Extracting {input_path} ...")
        contents_path.mkdir()
        subprocess.check_call(["tar", "-xzf", input_path, "--directory", contents_path])
    else:
        info(f"Copying {input_path} ...")
        shutil.copytree(input_path, contents_path)

    for path_to_wheel in wheel_paths(contents_path):
        unpack_wheel(path_to_wheel, work_dir)


def pack_all(work_dir, output_path):
    for path_to_wheel in wheel_paths(work_dir):
        pack_wheel(path_to_wheel, work_dir)

    info(f"Writing {output_path} ...")
    contents_path = work_dir / f"{VENDOR_ARCHIVE_NAME}-contents"
    assert contents_path.is_dir()
    subprocess.check_call(
        [
            "tar",
            "-czf",
            output_path,
            "--directory",
            contents_path,
            *[f.name for f in contents_path.glob("*")],
        ]
    )


def wheel_paths(root_path):
    yield from root_path.glob("**/*.whl")


def unpack_wheel(path_to_wheel, work_dir):
    wheel_name = path_to_wheel.name

    info(f"Unpacking {wheel_name} ...")
    subprocess.check_output(
        [sys.executable, "-m", "wheel", "unpack", "--dest", work_dir, path_to_wheel]
    )

    parts = wheel_name.split("-")
    wheel_id = f"{parts[0]}-{parts[1]}"

    wheel_contents_path = work_dir / wheel_id
    if not wheel_contents_path.is_dir():
        fatal(f"Unpacking {wheel_name} didn't work as expected")

    wheel_contents_path.rename(work_dir / f"{wheel_name}-contents")


def pack_wheel(path_to_wheel, work_dir):
    wheel_name = path_to_wheel.name
    dest_dir = path_to_wheel.parents[0]
    wheel_contents_path = work_dir / f"{wheel_name}-contents"
    assert wheel_contents_path.is_dir()

    info(f"Re-packing {wheel_name} ...")
    subprocess.check_output(
        [
            sys.executable,
            "-m",
            "wheel",
            "pack",
            "--dest-dir",
            dest_dir,
            wheel_contents_path,
        ]
    )


def read_cmd_lines(cmd):
    return subprocess.check_output(cmd, text=True).strip().splitlines()


def read_elf_cmd_lines(path_to_lib, pattern_to_read):
    result = []
    lines = read_cmd_lines(["readelf", "-d", path_to_lib])
    for line in lines:
        if pattern_to_read in line:
            result.append(line.strip().split()[4].strip("[]"))
    return result


def lib_paths(root_path, is_symlink=False):
    for ext in LIB_EXTENSIONS:
        for path_to_lib in root_path.glob(f"**/*{ext}"):
            if path_to_lib.is_symlink() == is_symlink:
                yield path_to_lib


def remove_lib_ext(lib_name):
    for ext in LIB_EXTENSIONS:
        if lib_name.endswith(ext):
            return lib_name[: -len(ext)]
    return lib_name


UNMODIFIED = 0
MODIFIED = 1


get_install_name = PlatformSpecific()


def get_install_name_Darwin(path_to_lib):
    lines = read_cmd_lines(["otool", "-D", path_to_lib])
    result = lines[1].strip() if len(lines) == 2 else None
    return result if result else None


def get_install_name_Linux(path_to_lib):
    lines = read_cmd_lines(["patchelf", "--print-soname", path_to_lib])
    result = lines[0].strip() if lines else None
    return result if result else None


set_install_name = PlatformSpecific()


def set_install_name_Darwin(path_to_lib, install_name):
    subprocess.check_call(["install_name_tool", "-id", install_name, path_to_lib])


def set_install_name_Linux(path_to_lib, install_name):
    subprocess.check_call(["patchelf", "--set-soname", install_name, path_to_lib])


DOT_PLUS_DIGITS = r"\.[0-9]+"
VERSION_PATTERN = re.compile("(" + DOT_PLUS_DIGITS + ")*$")


def get_proposed_name(path_to_lib, install_name, root_path):
    return path_to_lib.name


def fix_names(root_path, make_fatal=False, verbose=False):
    problems = []
    for path_to_lib in lib_paths(root_path):
        install_name = get_install_name(path_to_lib)
        proposed_name = get_proposed_name(path_to_lib, install_name, root_path)
        if not proposed_name:
            continue
        if path_to_lib.name != proposed_name or (
            install_name and install_name != proposed_name
        ):
            problems.append(
                {
                    "lib": path_to_lib,
                    "install_name": install_name,
                    "proposed_name": proposed_name,
                }
            )

    if not problems:
        checkmark("Checking names: all libs are well named.")
        return UNMODIFIED

    detail = json_dumps(problems, root_path) if verbose else None
    warn(
        f"Checking names: found {len(problems)} libs with name issues.",
        make_fatal=make_fatal,
        detail=detail,
    )

    for problem in problems:
        path_to_lib = problem["lib"]
        install_name = problem["install_name"]
        proposed_name = problem["proposed_name"]

        if path_to_lib.name != proposed_name:
            rename_path = path_to_lib.parents[0] / proposed_name
            path_to_lib.rename(rename_path)
            path_to_lib = rename_path

        if install_name != proposed_name:
            set_install_name(path_to_lib, path_to_lib.name)

    return MODIFIED


get_rpaths = PlatformSpecific()


def get_rpaths_Darwin(path_to_lib):
    rpaths = []
    lines = read_cmd_lines(["otool", "-l", path_to_lib])
    for i, line in enumerate(lines):
        if "RPATH" in line:
            rpaths.append(lines[i + 2].split()[1])
    return rpaths


def get_rpaths_Linux(path_to_lib):
    lines = read_cmd_lines(["patchelf", "--print-rpath", path_to_lib])
    if not lines or not lines[0]:
        return []
    return lines[0].split(":")


set_sole_rpaths = PlatformSpecific()


def set_sole_rpaths_Darwin(path_to_lib, rpaths):
    remove_all_rpaths_Darwin(path_to_lib)
    for rpath in rpaths:
        subprocess.check_call(["install_name_tool", "-add_rpath", rpath, path_to_lib])


def set_sole_rpaths_Linux(path_to_lib, rpaths):
    rpaths = ":".join(rpaths)
    subprocess.check_call(["patchelf", "--set-rpath", rpaths, path_to_lib])


remove_all_rpaths = PlatformSpecific()


def remove_all_rpaths_Darwin(path_to_lib):
    for rpath in get_rpaths_Darwin(path_to_lib):
        subprocess.check_call(
            ["install_name_tool", "-delete_rpath", rpath, path_to_lib]
        )


def remove_all_rpaths_Linux(path_to_lib):
    subprocess.check_call(["patchelf", "--remove-rpath", path_to_lib])


def get_eventual_path(path_to_lib):
    path_to_lib = str(path_to_lib)
    path_within_contents = path_to_lib.split("-contents/", maxsplit=1)[1]
    if ".whl-contents/" in path_to_lib:
        return SITE_PACKAGES_PREFIX + path_within_contents
    return path_within_contents


def propose_rpaths(eventual_lib_path):
    path_to_env_lib = os.path.relpath("env/lib/", Path(eventual_lib_path).parents[0])
    if path_to_env_lib == ".":
        return [LOADER_PATH]
    path_to_env_lib = path_to_env_lib.rstrip("/") + "/"
    return [LOADER_PATH, f"{LOADER_PATH}/{path_to_env_lib}"]


def fix_rpaths(root_path, make_fatal=False, verbose=False):
    problems = []
    for path_to_lib in lib_paths(root_path):
        actual_rpaths = get_rpaths(path_to_lib)
        eventual_path = get_eventual_path(path_to_lib)
        proposed_rpaths = propose_rpaths(eventual_path)
        if set(actual_rpaths) != set(proposed_rpaths):
            problems.append(
                {
                    "lib": path_to_lib,
                    "eventual_path": eventual_path,
                    "actual_rpaths": actual_rpaths,
                    "proposed_rpaths": proposed_rpaths,
                }
            )

    if not problems:
        checkmark("Checking rpaths: all libs have good rpaths.")
        return UNMODIFIED

    detail = json_dumps(problems, root_path) if verbose else None
    warn(
        f"Checking rpaths: found {len(problems)} libs with rpath issues.",
        make_fatal=make_fatal,
        detail=detail,
    )

    for problem in problems:
        set_sole_rpaths(problem["lib"], problem["proposed_rpaths"])

    return MODIFIED


get_deps = PlatformSpecific()


def get_deps_Darwin(path_to_lib):
    deps = []
    lines = read_cmd_lines(["otool", "-L", path_to_lib])
    for line in lines[1:]:
        dep = line.strip().split()[0]
        if any(dep.endswith(ext) for ext in LIB_EXTENSIONS):
            deps.append(dep)
    return deps


def get_deps_Linux(path_to_lib):
    return read_elf_cmd_lines(path_to_lib, "(NEEDED)")


def rpath_dep(dep):
    return RPATH_PREFIX + Path(dep).name


def get_proposed_dep(dep, all_satisfied_deps):
    # Find an exact match:
    dep_name = Path(dep).name
    if dep_name in all_satisfied_deps:
        return rpath_dep(dep)
    if dep_name in UNSATISFIED_DEPS_ALLOW_SET:
        return dep

    # Search for similar looking deps:
    n1 = remove_lib_ext(dep_name)
    for satisfied_dep in all_satisfied_deps:
        n2 = remove_lib_ext(satisfied_dep)
        if n1.startswith(n2) or n2.startswith(n1):
            return rpath_dep(satisfied_dep)

    return None


def list_unsatisfied_deps(satisfied_deps, unsatisfied_deps):
    output = []
    for dep in sorted(
        unsatisfied_deps | satisfied_deps, key=lambda dep: Path(dep).name
    ):
        prefix = "🆗  " if dep in satisfied_deps else "❌  "
        output.append(prefix + dep)
    return "Unsatisfied dependencies:\n" + "\n".join(output)


change_dep = PlatformSpecific()


def change_dep_Darwin(path_to_lib, old_dep, new_dep):
    subprocess.check_call(
        ["install_name_tool", "-change", old_dep, new_dep, path_to_lib]
    )


def change_dep_Linux(path_to_lib, old_dep, new_dep):
    subprocess.check_call(
        ["patchelf", "--replace-needed", old_dep, new_dep, path_to_lib]
    )


def fix_deps(root_path, make_fatal=False, verbose=False):
    all_satisfied_deps = set()
    for path_to_lib in lib_paths(root_path):
        all_satisfied_deps.add(path_to_lib.name)

    fatal_problems = []
    fixable_problems = []

    all_unsatisfied_deps = set()

    for path_to_lib in lib_paths(root_path):
        unsatisfied_deps = []
        deps_to_change = []

        deps = get_deps(path_to_lib)
        for dep in deps:
            if dep == get_install_name(path_to_lib):
                continue
            proposed_dep = get_proposed_dep(dep, all_satisfied_deps)
            if not proposed_dep:
                unsatisfied_deps.append(dep)
            elif dep != proposed_dep:
                deps_to_change.append([dep, proposed_dep])

        if unsatisfied_deps:
            fatal_problems.append(
                {"lib": path_to_lib, "deps": deps, "unsatisfied_deps": unsatisfied_deps}
            )
            all_unsatisfied_deps.update(unsatisfied_deps)
        if deps_to_change:
            fixable_problems.append(
                {"lib": path_to_lib, "deps": deps, "deps_to_change": deps_to_change}
            )

    if fatal_problems:
        detail = json_dumps(fatal_problems, root_path)
        detail += "\n" + list_unsatisfied_deps(all_satisfied_deps, all_unsatisfied_deps)
        fatal(
            f"Checking deps: found {len(fatal_problems)} libs with unsatisfied_deps.\n{detail}"
        )
    else:
        checkmark("Checking deps: all deps are satisfied or allowed.")

    if not fixable_problems:
        checkmark("Checking deps: all lib deps are well formatted.")
        return UNMODIFIED

    detail = json_dumps(fixable_problems, root_path) if verbose else None
    warn(
        f"Checking deps: found {len(fixable_problems)} libs with dep formatting issues.",
        make_fatal=make_fatal,
        detail=detail,
    )

    for problem in fixable_problems:
        path_to_lib = problem["lib"]
        for old_dep, new_dep in problem["deps_to_change"]:
            change_dep(path_to_lib, old_dep, new_dep)

    return MODIFIED


def fix_everything(input_path, output_path):
    if not input_path.resolve().exists():
        fatal(f"Path does not exist {input_path}")

    if output_path:
        if output_path.is_dir():
            output_path = output_path / VENDOR_ARCHIVE_NAME
        if output_path.exists():
            fatal(f"Cannot write vendor archive to {output_path} - already exists")
    else:
        print("(Running in dry-run mode since no OUTPUT_PATH was supplied.)")

    with tempfile.TemporaryDirectory() as work_dir:
        work_dir = Path(work_dir)
        unpack_all(input_path, work_dir)

        status = UNMODIFIED
        status |= fix_names(work_dir)
        status |= fix_rpaths(work_dir)
        status |= fix_deps(work_dir)

        if status == MODIFIED:
            checkmark("Finished fixing.\n")
            info("Checking everything was fixed ...")
            kwargs = {"make_fatal": True, "verbose": True}
            fix_names(work_dir, **kwargs)
            fix_rpaths(work_dir, **kwargs)
            fix_deps(work_dir, **kwargs)
        else:
            checkmark("Nothing to change.\n")

        if output_path:
            pack_all(work_dir, output_path)
            checkmark(f"Wrote fixed archive to {output_path}")
        elif status == MODIFIED:
            warn("Archive was fixed, but not writing anywhere due to dry-run mode.")
            sys.exit(1)


# Make foo_{PLATFORM} functions work:
for symbol in list(globals().keys()):
    if isinstance(globals()[symbol], PlatformSpecific):
        globals()[symbol] = globals()[f"{symbol}_{PLATFORM}"]


args = sys.argv[1:]
if len(args) not in (1, 2):
    print(USAGE.strip(), file=sys.stderr)
    sys.exit(1)

input_path = Path(args[0])
output_path = Path(args[1]) if len(args) == 2 else None

fix_everything(input_path, output_path)
