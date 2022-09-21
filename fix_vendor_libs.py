#!/usr/bin/python3

import click
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile


# Checks / fixes every lib in a vendor-Darwin.tar.gz archive according to the following rules.
# This includes the libraries that are currently embedded inside wheels
# - check_for_unsatisfied_deps:
#   All library deps must be contained in vendor-Darwin.tar.gz unless they are on the UNSATISFIED_DEPS_ALLOW_LIST.
# - check_ids:
#   All libraries must have IDs that are simply their own filename - not any other kind of path - or no ID at all.
# - check_rpaths:
#   All libraries must have an RPATH of @loader_path to ensure they can find deps in the same folder.
#   All libraries that are not / will not be in env/lib must also have an RPATH of @loader_path/<path-to-env-lib>,
#   (using the library's eventual install location for libraries that are currently inside wheels)
# - check_dep_paths:
#   All deps from one library inside the archive to another library inside the archive must be specified in
#   the following way: @rpath/<name-of-library>. Deps to a library outside the archive are left unchanged.


SITE_PACKAGES_PREFIX = "env/lib/python3.x/site-packages/"

if platform.system() == "Darwin":
    VENDOR_ARCHIVE_NAME = "vendor-Darwin.tar.gz"
    LOADER_PATH = "@loader_path"
    RPATH = "@rpath"
elif platform.system() == "Linux":
    # TODO - find the details of how this should work on unix, and use patchelf or similar to fix libraries
    VENDOR_ARCHIVE_NAME = "vendor-Linux.tar.gz"
    LOADER_PATH = "$ORIGIN"

UNSATISFIED_DEPS_ALLOW_LIST = [
    "libSystem.B.dylib",
    "libbrotlicommon.1.dylib",
    "libc++.1.dylib",
    "libiconv.2.dylib",
    "libresolv.9.dylib",
    "libsasl2.2.dylib",
    "libz.1.dylib",
]


def name(path):
    return Path(path).name


def lib_paths(root_path):
    for ext in ("so", "dylib"):
        yield from root_path.glob(f"**/*.{ext}")


def wheel_paths(root_path):
    yield from root_path.glob("**/*.whl")


def get_rpaths(path_to_lib):
    rpaths = []
    lines = (
        subprocess.check_output(["otool", "-l", path_to_lib], text=True)
        .strip()
        .splitlines()
    )
    for i, line in enumerate(lines):
        if "RPATH" in line:
            rpaths.append(lines[i + 2].split()[1])
    return rpaths


def delete_all_rpaths(path_to_lib):
    rpaths = get_rpaths(path_to_lib)
    for rpath in rpaths:
        subprocess.check_call(
            ["install_name_tool", "-delete_rpath", rpath, path_to_lib]
        )


def set_sole_rpaths(path_to_lib, rpaths):
    delete_all_rpaths(path_to_lib)
    for rpath in rpaths:
        subprocess.check_call(["install_name_tool", "-add_rpath", rpath, path_to_lib])


LIB_EXT_PATTERN = re.compile(r".*\.(so|dylib)")


def get_lib_deps(path_to_lib):
    deps = []
    lines = (
        subprocess.check_output(["otool", "-L", path_to_lib], text=True)
        .strip()
        .splitlines()
    )
    for line in lines[1:]:
        dep = line.strip().split()[0]
        if LIB_EXT_PATTERN.fullmatch(dep):
            deps.append(dep)
    return deps


def change_lib_dep(path_to_lib, old_dep, new_dep):
    subprocess.check_call(
        ["install_name_tool", "-change", old_dep, new_dep, path_to_lib]
    )


def get_lib_id(path_to_lib):
    lines = (
        subprocess.check_output(["otool", "-D", path_to_lib], text=True)
        .strip()
        .splitlines()
    )
    if len(lines) == 2:
        return lines[1].strip()
    return None


def set_lib_id(path_to_lib, new_id):
    subprocess.check_call(["install_name_tool", "-id", new_id, path_to_lib])


def get_lib_summary(path_to_lib, root_path):
    lib_name = name(path_to_lib)
    rel_path_to_lib = os.path.relpath(path_to_lib, root_path)
    return {
        "name": lib_name,
        "id": get_lib_id(path_to_lib),
        "symlink": path_to_lib.is_symlink(),
        "location": str(rel_path_to_lib),
        "rpaths": get_rpaths(path_to_lib),
        "deps": get_lib_deps(path_to_lib),
    }


def non_symlinks(lib_summaries):
    return (s for s in lib_summaries.values() if not s["symlink"])


def get_path_to_lib(lib_summary, root_path):
    if "wheelLocation" in lib_summary:
        return (
            get_extracted_wheel_path(lib_summary["wheelLocation"], root_path)
            / lib_summary["withinWheelLocation"]
        )
    return root_path / lib_summary["location"]


def get_extracted_wheel_path(path_to_wheel, root_path):
    wheel_name = name(path_to_wheel)
    parts = wheel_name.split("-")
    wheel_id = f"{parts[0]}-{parts[1]}"

    extracted_wheel_path = (root_path / f"../{wheel_id}").resolve()
    if extracted_wheel_path.is_dir():
        return extracted_wheel_path

    click.echo(f"Unpacking {wheel_name} ...")
    subprocess.check_output(
        [
            sys.executable,
            "-m",
            "wheel",
            "unpack",
            "--dest",
            root_path / "../",
            path_to_wheel,
        ]
    )

    if not extracted_wheel_path.is_dir():
        raise RuntimeError("Wheel didn't unpack as expected")
    return extracted_wheel_path


def get_wheel_lib_summaries(path_to_wheel, orig_root_path):
    rel_path_to_wheel = os.path.relpath(path_to_wheel, orig_root_path)

    root_path = get_extracted_wheel_path(path_to_wheel, orig_root_path)

    for lib_path in lib_paths(root_path):
        summary = get_lib_summary(lib_path, root_path)
        summary["wheelLocation"] = rel_path_to_wheel
        summary["withinWheelLocation"] = summary["location"]
        summary["location"] = SITE_PACKAGES_PREFIX + summary["location"]
        yield summary


def get_all_lib_summaries(root_path):
    lib_summaries = {}
    for lib_path in lib_paths(root_path):
        summary = get_lib_summary(lib_path, root_path)
        name = summary["name"]
        if name in lib_summaries:
            raise RuntimeError(f"Found a second lib called {name}")
        lib_summaries[name] = summary

    for wheel_path in wheel_paths(root_path):
        for summary in get_wheel_lib_summaries(wheel_path, root_path):
            name = summary["name"]
            if name in lib_summaries:
                raise RuntimeError(f"Found a second lib (in a wheel) called {name}")
            lib_summaries[name] = summary

    return lib_summaries


def check_for_unsatisfied_deps(lib_summaries, strict=False, verbose=False):
    all_deps = set()
    for summary in lib_summaries.values():
        all_deps.update(name(dep) for dep in summary["deps"])

    unsatisfied_deps = (
        all_deps - lib_summaries.keys() - set(UNSATISFIED_DEPS_ALLOW_LIST)
    )
    if not unsatisfied_deps:
        click.secho("Checking deps: all deps are satisfied.", bold=True)
        return True

    problem_summaries = [
        s
        for s in lib_summaries.values()
        if any(name(dep) in unsatisfied_deps for dep in s["deps"])
    ]
    click.secho(
        f"Checking deps: {len(unsatisfied_deps)} unsatisfied dependencies found",
        bold=True,
        fg="red",
    )
    if verbose:
        lines = json.dumps(problem_summaries, indent=2).splitlines()
        for line in lines:
            fg = "red" if any(dep in line for dep in unsatisfied_deps) else None
            click.secho(line, fg=fg)
        click.secho(
            f"All {len(unsatisfied_deps)} unsatisfied dependencies:",
            bold=True,
            fg="red",
        )
        for dep in sorted(unsatisfied_deps):
            click.secho(dep, fg="red")

    if strict:
        sys.exit(1)
    return False


def check_ids(lib_summaries, strict=False, verbose=False):
    problem_summaries = []
    for summary in non_symlinks(lib_summaries):
        if summary["id"] and summary["id"] != summary["name"]:
            problem_summaries.append(summary)

    if not problem_summaries:
        click.secho("Checking IDs: all IDs are good.", bold=True)
        return True

    click.secho(
        f"Checking IDs: {len(problem_summaries)} of {len(lib_summaries)} libs have ID issues",
        bold=True,
        fg="red",
    )
    if verbose:
        lines = json.dumps(problem_summaries, indent=2).splitlines()
        for line in lines:
            fg = "red" if '"id":' in line else None
            click.secho(line, fg=fg)
    if strict:
        sys.exit(1)
    return False


def fix_ids(lib_summaries, root_path):
    todo_list = []
    for summary in non_symlinks(lib_summaries):
        if summary["id"] and summary["id"] != summary["name"]:
            todo_list.append(get_path_to_lib(summary, root_path))

    if todo_list:
        click.secho(f"Fixing IDs for {len(todo_list)} libs...")
    for path_to_lib in todo_list:
        set_lib_id(path_to_lib, name(path_to_lib))


def choose_good_rpaths(lib_summary):
    path_to_env_lib = os.path.relpath(
        "env/lib/", Path(lib_summary["location"]).parents[0]
    )
    if path_to_env_lib == ".":
        return [LOADER_PATH]
    path_to_env_lib = path_to_env_lib.rstrip("/") + "/"
    return [LOADER_PATH, f"{LOADER_PATH}/{path_to_env_lib}"]


def check_rpaths(lib_summaries, strict=False, verbose=False):
    problem_summaries = []
    for summary in non_symlinks(lib_summaries):
        if set(summary["rpaths"]) != set(choose_good_rpaths(summary)):
            problem_summaries.append(summary)

    if not problem_summaries:
        click.secho("Checking rpaths: all rpaths are good.", bold=True)
        return True

    click.secho(
        f"Checking rpaths: {len(problem_summaries)} of {len(lib_summaries)} libs have rpath issues",
        bold=True,
        fg="red",
    )
    if verbose:
        lines = json.dumps(problem_summaries, indent=2).splitlines()
        fg = None
        for line in lines:
            fg = "red" if "rpaths" in line else fg
            click.secho(line, fg=fg)
            fg = None if "]" in line else fg

    if strict:
        sys.exit(1)
    return False


def fix_rpaths(lib_summaries, root_path):
    todo_list = []
    for summary in non_symlinks(lib_summaries):
        good_rpaths = choose_good_rpaths(summary)
        if set(summary["rpaths"]) != set(good_rpaths):
            todo_list.append((get_path_to_lib(summary, root_path), good_rpaths))

    if todo_list:
        click.secho(f"Fixing rpaths for {len(todo_list)} libs...")
    for path_to_lib, good_rpaths in todo_list:
        set_sole_rpaths(path_to_lib, good_rpaths)


def is_good_dep(dep, parent_summary, all_lib_summaries):
    dep_name = name(dep)
    if dep == parent_summary["name"]:
        return True
    if dep_name not in all_lib_summaries and dep_name in UNSATISFIED_DEPS_ALLOW_LIST:
        return True
    return dep == f"{RPATH}/{dep_name}"


def check_dep_paths(lib_summaries, strict=False, verbose=False):
    problem_summaries = []
    all_bad_deps = set()
    for summary in non_symlinks(lib_summaries):
        bad_deps = [
            dep
            for dep in summary["deps"]
            if not is_good_dep(dep, summary, lib_summaries)
        ]
        if bad_deps:
            problem_summaries.append(summary)
            all_bad_deps.update(bad_deps)

    if not problem_summaries:
        click.secho("Checking dep paths: all dep paths are good.", bold=True)
        return True

    click.secho(
        f"Checking dep paths: {len(problem_summaries)} of {len(lib_summaries)} libs have dep path issues",
        bold=True,
        fg="red",
    )
    if verbose:
        lines = json.dumps(problem_summaries, indent=2).splitlines()
        for line in lines:
            fg = "red" if (any(dep in line for dep in all_bad_deps)) else None
            click.secho(line, fg=fg)

    if strict:
        sys.exit(1)
    return False


def fix_dep_paths(lib_summaries, root_path):
    todo_list = []
    for summary in non_symlinks(lib_summaries):
        path_to_lib = get_path_to_lib(summary, root_path)
        for dep in summary["deps"]:
            if not is_good_dep(dep, summary, lib_summaries):
                todo_list.append((path_to_lib, dep))

    if todo_list:
        click.secho(f"Fixing {len(todo_list)} dependency paths...")

    for path_to_lib, dep in todo_list:
        change_lib_dep(path_to_lib, dep, f"{RPATH}/{name(dep)}")


def repack_wheels(lib_summaries, root_path):
    paths_to_wheels = set()
    for summary in lib_summaries.values():
        if "wheelLocation" in summary:
            paths_to_wheels.add(root_path / summary["wheelLocation"])

    for path_to_wheel in paths_to_wheels:
        wheel_name = path_to_wheel.name
        extracted_wheel_path = get_extracted_wheel_path(path_to_wheel, root_path)
        click.echo(f"Re-packing {wheel_name} ...")
        subprocess.check_output(
            [
                sys.executable,
                "-m",
                "wheel",
                "pack",
                "--dest-dir",
                path_to_wheel.parents[0],
                extracted_wheel_path,
            ]
        )


def fix_vendor_libs(input_path, output_path):
    if not input_path.resolve().exists():
        click.echo(f"Path does not exist {input_path}", err=True)
        sys.exit(1)

    if output_path:
        if output_path.is_dir():
            output_path = output_path / VENDOR_ARCHIVE_NAME

        if output_path.exists():
            click.echo(f"Cannot write vendor archive to {output_path}", err=True)
            sys.exit(1)

    else:
        click.echo("(Running in dry-run mode since no OUTPUT_PATH was supplied.)")

    with tempfile.TemporaryDirectory() as root_path:
        root_path = Path(root_path)

        if input_path.is_file():
            root_path = root_path / "extracted"
            root_path.mkdir()
            print(f"Extracting {input_path} ...")
            subprocess.check_call(["tar", "-xzf", input_path, "--directory", root_path])
        else:
            print(f"Copying {input_path}...")
            shutil.copytree(input_path, root_path / input_path.name)
            root_path = root_path / input_path.name

        lib_summaries = get_all_lib_summaries(root_path)

        okay = True
        okay &= check_for_unsatisfied_deps(lib_summaries, strict=True, verbose=True)
        okay &= check_ids(lib_summaries)
        okay &= check_rpaths(lib_summaries)
        okay &= check_dep_paths(lib_summaries)

        if okay:
            click.secho("Nothing to fix! Leaving unchanged.")
        else:
            fix_ids(lib_summaries, root_path)
            # Fixing the IDs can fix the apparent deps, so we reload the lib_summaries.
            lib_summaries = get_all_lib_summaries(root_path)

            fix_rpaths(lib_summaries, root_path)
            fix_dep_paths(lib_summaries, root_path)

            lib_summaries = get_all_lib_summaries(root_path)

            check_for_unsatisfied_deps(lib_summaries, strict=True, verbose=True)
            check_ids(lib_summaries, strict=True, verbose=True)
            check_rpaths(lib_summaries, strict=True, verbose=True)
            check_dep_paths(lib_summaries, strict=True, verbose=True)

            repack_wheels(lib_summaries, root_path)

        if output_path:
            click.secho(f"Writing fixed vendor archive to {output_path} ...", bold=True)
            subprocess.check_call(
                [
                    "tar",
                    "-czf",
                    output_path,
                    "--directory",
                    root_path,
                    *[f.name for f in root_path.glob("*")],
                ]
            )
        elif not okay:
            click.secho(
                "Dry-run fix succeeded, but not actually fixing due to dry-run mode.",
                bold=True,
            )
            sys.exit(1)


args = sys.argv[1:]

USAGE = """
Usage: fix_vendor_libs INPUT_PATH [OUTPUT_PATH]")
    INPUT_PATH is the path to a vendor archive (eg vendor-Darwin.tar.gz),
        or a path to the uncompressed contents of a vendor archive.
    OUTPUT_PATH the path to which the fixed vendor archive is written.
        If not supplied, fix_vendor_libs runs in a dry-run mode where it fixes
        the archive in a temp directory, but doesn't output it anywhere.
"""

if len(args) not in (1, 2):
    click.echo(USAGE.strip(), err=True)
    sys.exit(1)

input_path = Path(args[0])
output_path = Path(args[1]) if len(args) == 2 else None

fix_vendor_libs(input_path, output_path)
