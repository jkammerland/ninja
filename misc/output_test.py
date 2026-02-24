#!/usr/bin/env python3

"""Runs ./ninja and checks if the output is correct.

In order to simulate a smart terminal it uses the 'script' command.
"""

import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from textwrap import dedent
import typing as T

default_env = dict(os.environ)
default_env.pop('NINJA_STATUS', None)
default_env.pop('CLICOLOR_FORCE', None)
default_env['TERM'] = ''
NINJA_PATH = os.path.abspath('./ninja')

def remove_non_visible_lines(raw_output: bytes) -> str:
  # When running in a smart terminal, Ninja uses CR (\r) to
  # return the cursor to the start of the current line, prints
  # something, then uses `\x1b[K` to clear everything until
  # the end of the line.
  #
  # Thus printing 'FOO', 'BAR', 'ZOO' on the same line, then
  # jumping to the next one results in the following output
  # on Posix:
  #
  # '\rFOO\x1b[K\rBAR\x1b[K\rZOO\x1b[K\r\n'
  #
  # The following splits the output at both \r, \n and \r\n
  # boundaries, which gives:
  #
  #  [ '\r', 'FOO\x1b[K\r', 'BAR\x1b[K\r', 'ZOO\x1b[K\r\n' ]
  #
  decoded_lines = raw_output.decode('utf-8').splitlines(True)

  # Remove any item that ends with a '\r' as this means its
  # content will be overwritten by the next item in the list.
  # For the previous example, this gives:
  #
  #  [ 'ZOO\x1b[K\r\n' ]
  #
  final_lines = [ l for l in decoded_lines if not l.endswith('\r') ]

  # Return a single string that concatenates all filtered lines
  # while removing any remaining \r in it. Needed to transform
  # \r\n into \n.
  #
  #  "ZOO\x1b[K\n'
  #
  return ''.join(final_lines).replace('\r', '')

class BuildDir:
    def __init__(self, build_ninja: str):
        self.build_ninja = dedent(build_ninja)
        self.d = None

    def __enter__(self):
        self.d = tempfile.TemporaryDirectory()
        with open(os.path.join(self.d.name, 'build.ninja'), 'w') as f:
            f.write(self.build_ninja)
            f.flush()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.d.cleanup()

    @property
    def path(self) -> str:
        return os.path.realpath(self.d.name)


    def run(
        self,
        flags: T.Optional[str] = None,
        pipe: bool = False,
        raw_output: bool = False,
        env: T.Dict[str, str] = default_env,
        print_err_output = True,
    ) -> str:
        """Run Ninja command, and get filtered output.

        Args:
          flags: Extra arguments passed to Ninja.

          pipe: set to True to run Ninja in a non-interactive terminal.
            If False (the default), this runs Ninja in a pty to simulate
            a smart terminal (this feature cannot work on Windows!).

          raw_output: set to True to return the raw, unfiltered command
            output.

          env: Optional environment dictionary to run the command in.

          print_err_output: set to False if the test expects ninja to print
            something to stderr. (Otherwise, an error message from Ninja
            probably represents a failed test.)

        Returns:
          A UTF-8 string corresponding to the output (stdout only) of the
          Ninja command. By default, partial lines that were overwritten
          are removed according to the rules described in the comments
          below.
        """
        ninja_cmd = '{} {}'.format(NINJA_PATH, flags if flags else '')
        try:
            if pipe:
                output = subprocess.check_output(
                    [ninja_cmd], shell=True, cwd=self.d.name, env=env)
            elif platform.system() == 'Darwin':
                output = subprocess.check_output(['script', '-q', '/dev/null', 'bash', '-c', ninja_cmd],
                                                 cwd=self.d.name, env=env)
            else:
                output = subprocess.check_output(['script', '-qfec', ninja_cmd, '/dev/null'],
                                                 cwd=self.d.name, env=env)
        except subprocess.CalledProcessError as err:
            if print_err_output:
              sys.stdout.buffer.write(err.output)
            err.cooked_output = remove_non_visible_lines(err.output)
            raise err

        if raw_output:
            return output.decode('utf-8')
        return remove_non_visible_lines(output)

def run(
    build_ninja: str,
    flags: T.Optional[str] = None,
    pipe: bool = False,
    raw_output: bool = False,
    env: T.Dict[str, str] = default_env,
    print_err_output = True,
) -> str:
    """Run Ninja with a given build plan in a temporary directory.
    """
    with BuildDir(build_ninja) as b:
        return b.run(flags, pipe, raw_output, env, print_err_output)

@unittest.skipIf(platform.system() == 'Windows', 'These test methods do not work on Windows')
class Output(unittest.TestCase):
    BUILD_SIMPLE_ECHO = '\n'.join((
        'rule echo',
        '  command = printf "do thing"',
        '  description = echo $out',
        '',
        'build a: echo',
        '',
    ))

    def _test_expected_error(self, plan: str, flags: T.Optional[str],expected: str,
                             *args, exit_code: T.Optional[int]=None, **kwargs)->None:
        """Run Ninja with a given plan and flags, and verify its cooked output against an expected content.
        All *args and **kwargs are passed to the `run` function
        """
        actual = ''
        kwargs['print_err_output'] = False
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            run(plan, flags, *args,  **kwargs)
        actual = cm.exception.cooked_output
        if exit_code is not None:
            self.assertEqual(cm.exception.returncode, exit_code)
        self.assertEqual(expected, actual)

    def _create_file_and_advance_dir_mtime(
        self,
        directory: str,
        filename: str,
        content: str = '',
        timeout_secs: float = 5.0,
    ) -> str:
        """Create |filename| inside |directory| and ensure the directory mtime ticks."""
        before = os.stat(directory).st_mtime_ns
        path = os.path.join(directory, filename)
        deadline = time.time() + timeout_secs
        while True:
            with open(path, 'w') as f:
                f.write(content)
            if os.stat(directory).st_mtime_ns != before:
                return path
            os.unlink(path)
            if time.time() >= deadline:
                self.fail(
                    f"directory mtime for '{directory}' did not advance")
            time.sleep(0.02)

    def _escape_ninja_path(self, path: str) -> str:
        return path.replace('$', '$$').replace(':', '$:').replace(' ', '$ ')

    def _assert_single_manifest_restart(self, output: str) -> None:
        self.assertEqual(
            output.count(
                'regeneration complete; restarting with updated manifest...'),
            1)
        self.assertEqual(output.count('Re-checking...'), 1)
        self.assertIn('ninja: no work to do.', output)

    def _run_ninja_in_dir(
        self,
        cwd: str,
        args: T.Optional[T.List[str]] = None,
    ) -> str:
        cmd = [NINJA_PATH]
        if args:
            cmd.extend(args)
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=default_env,
            capture_output=True,
            check=False,
            text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, '')
        return proc.stdout

    def test_issue_1418(self) -> None:
        self.assertEqual(run(
'''rule echo
  command = sleep $delay && echo $out
  description = echo $out

build a: echo
  delay = 3
build b: echo
  delay = 2
build c: echo
  delay = 1
''', '-j3'),
'''[1/3] echo c\x1b[K
c
[2/3] echo b\x1b[K
b
[3/3] echo a\x1b[K
a
''')

    def test_issue_1214(self) -> None:
        print_red = '''rule echo
  command = printf '\x1b[31mred\x1b[0m'
  description = echo $out

build a: echo
'''
        # Only strip color when ninja's output is piped.
        self.assertEqual(run(print_red),
'''[1/1] echo a\x1b[K
\x1b[31mred\x1b[0m
''')
        self.assertEqual(run(print_red, pipe=True),
'''[1/1] echo a
red
''')
        # Even in verbose mode, colors should still only be stripped when piped.
        self.assertEqual(run(print_red, flags='-v'),
'''[1/1] printf '\x1b[31mred\x1b[0m'
\x1b[31mred\x1b[0m
''')
        self.assertEqual(run(print_red, flags='-v', pipe=True),
'''[1/1] printf '\x1b[31mred\x1b[0m'
red
''')

        # CLICOLOR_FORCE=1 can be used to disable escape code stripping.
        env = default_env.copy()
        env['CLICOLOR_FORCE'] = '1'
        self.assertEqual(run(print_red, pipe=True, env=env),
'''[1/1] echo a
\x1b[31mred\x1b[0m
''')

    def test_issue_1966(self) -> None:
        self.assertEqual(run(
'''rule cat
  command = cat $rspfile $rspfile > $out
  rspfile = cat.rsp
  rspfile_content = a b c

build a: cat
''', '-j3'),
'''[1/1] cat cat.rsp cat.rsp > a\x1b[K
''')

    def test_issue_2499(self) -> None:
        # This verifies that Ninja prints its status line updates on a single
        # line when running in a smart terminal, and when commands do not have
        # any output. Get the raw command output which includes CR (\r) codes
        # and all content that was printed by Ninja.
        self.assertEqual(run(
'''rule touch
  command = touch $out

build foo: touch
build bar: touch foo
build zoo: touch bar
''', flags='-j1 zoo', raw_output=True).split('\r'),
            [
                '',
                '[0/3] touch foo\x1b[K',
                '[1/3] touch foo\x1b[K',
                '[1/3] touch bar\x1b[K',
                '[2/3] touch bar\x1b[K',
                '[2/3] touch zoo\x1b[K',
                '[3/3] touch zoo\x1b[K',
                '\n',
            ])

    def test_issue_1336(self) -> None:
        # In non-TTY mode, console edges should report progress when finished
        # (like non-console edges) so [%f/%t] reaches [N/N].
        self.assertEqual(run(
'''rule touch
  command = touch $out
  description = touch $out

rule install
  command = touch $out
  description = Installing files.
  pool = console

build out: touch
build install: install out
''', pipe=True),
'''[1/2] touch out
[2/2] Installing files.
''')

    def test_issue_1336_dumb_tty(self) -> None:
        # In non-smart TTY mode, console edges should still end at [N/N].
        env = default_env.copy()
        env['TERM'] = 'dumb'
        self.assertEqual(run(
'''rule touch
  command = touch $out
  description = touch $out

rule install
  command = printf "install\\n"
  description = Installing files.
  pool = console

build out: touch
build install: install out
''', env=env),
'''[1/2] touch out
[1/2] Installing files.
install
[2/2] Installing files.
''')

    def test_issue_1336_dumb_tty_failure(self) -> None:
        # In non-smart TTY mode, a failing final console edge should also
        # print a final [N/N] status before the error output.
        py = sys.executable
        env = default_env.copy()
        env['TERM'] = 'dumb'
        self._test_expected_error(
            f'''rule touch
  command = touch $out
  description = touch $out

rule install
  command = {py} -c 'import sys; print("install failed"); sys.exit(127)'
  description = Installing files.
  pool = console

build out: touch
build install: install out
''', None,
            f'''[1/2] touch out
[1/2] Installing files.
install failed
[2/2] Installing files.
FAILED: [code=127] install \n{py} -c 'import sys; print("install failed"); sys.exit(127)'
ninja: build stopped: subcommand failed.
''',
            exit_code=127, env=env,
        )

    def test_issue_1336_dumb_tty_interrupt(self) -> None:
        # Interrupted console edges in non-smart TTY mode should keep the
        # interrupted-by-user behavior unchanged.
        py = sys.executable
        env = default_env.copy()
        env['TERM'] = 'dumb'
        self._test_expected_error(
            f'''rule interrupt
  command = {py} -c 'import sys; sys.exit(130)'
  description = Interrupting...
  pool = console

build stop: interrupt
''', None,
            '''[0/1] Interrupting...
ninja: build stopped: interrupted by user.
''',
            exit_code=130, env=env,
        )

    def test_issue_1336_dumb_tty_empty_status_format(self) -> None:
        # When NINJA_STATUS is disabled, avoid extra completion-only status
        # output in dumb TTY mode.
        env = default_env.copy()
        env['TERM'] = 'dumb'
        env['NINJA_STATUS'] = ''
        self.assertEqual(run(
'''rule touch
  command = touch $out
  description = touch $out

rule install
  command = printf "install\\n"
  description = Installing files.
  pool = console

build out: touch
build install: install out
''', env=env),
'''touch out
Installing files.
install
''')

    def test_issue_1336_smart_terminal(self) -> None:
        # In smart terminals, console edges should also report completion so
        # the final visible status reaches [N/N].
        self.assertEqual(run(
'''rule touch
  command = touch $out
  description = touch $out

rule install
  command = printf "install\\n"
  description = Installing files.
  pool = console

build out: touch
build install: install out
'''),
'''[1/2] Installing files.\x1b[K
install
[2/2] Installing files.\x1b[K
''')

    def test_issue_2507(self) -> None:
        # A single ninja invocation may run a manifest-check phase before the
        # user-requested build. Status counters must not leak across phases.
        self.assertEqual(run(
'''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch
default out
''', pipe=True),
'''[1/1] Re-checking...
ninja: manifest check complete; building requested targets...
[1/1] touch out
''')

    def test_manifest_check_messages_hidden_in_quiet_mode(self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule emit
  command = touch $out && printf "done\\n"
  description = emit

build build.ninja: verify
build out: emit src/a.cpp
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            first = b.run(flags='--quiet', pipe=True)
            self.assertEqual(first, 'done\n')
            self.assertNotIn('manifest check complete; building requested targets...',
                             first)
            self.assertNotIn('regeneration complete; restarting with updated manifest...',
                             first)

            self.assertEqual(b.run(flags='--quiet', pipe=True), '')

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
            third = b.run(flags='--quiet', pipe=True)
            self.assertEqual(third, '')
            self.assertNotIn('manifest check complete; building requested targets...',
                             third)
            self.assertNotIn('regeneration complete; restarting with updated manifest...',
                             third)

    def test_manifest_check_with_directory_input(self) -> None:
        # Generator edges can depend on directories and re-run when
        # directory entries change.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify src
build out: touch
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)

            first = b.run(pipe=True)
            self.assertIn('Re-checking...', first)
            self.assertIn('manifest check complete; building requested targets...',
                          first)
            self.assertIn('touch out', first)

            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            # Touch directory mtime by adding a new entry.
            self._create_file_and_advance_dir_mtime(src_dir, 'new.cc')

            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('manifest check complete; building requested targets...',
                          third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn('touch out', third)

    def test_manifest_check_on_source_directory_entry_change(self) -> None:
        # If source file directories change, re-check the manifest before the
        # normal build phase. This allows generators to pick up plain GLOB
        # add/remove edits without explicit directory inputs.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule cc
  command = touch $out
  description = CXX $out

build build.ninja: verify
build a.o: cc src/a.cpp
default a.o
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            self.assertEqual(b.run(pipe=True), '[1/1] CXX a.o\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            # Add a file in the same source directory; this should trigger
            # a manifest check phase before concluding there's no work.
            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')

            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn('CXX a.o', third)

    def test_manifest_check_prunes_builddir_relative_inferred_dirs_out_of_source(
            self) -> None:
        # In mixed relative/absolute path aliases, generated build-local inputs
        # should not be inferred as watched source directories.
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, 'srcroot')
            build_dir = os.path.join(root, 'build')
            os.mkdir(source_root)
            os.mkdir(build_dir)

            source_dir = os.path.join(source_root, 'src')
            os.mkdir(source_dir)
            source_file = os.path.join(source_dir, 'a.cpp')
            with open(source_file, 'w'):
                pass

            generated_dir = os.path.join(build_dir, 'gen')
            os.mkdir(generated_dir)
            generated_file = os.path.join(generated_dir, 'generated.cpp')
            with open(generated_file, 'w'):
                pass

            generated_abs = self._escape_ninja_path(
                generated_file.replace('\\', '/'))
            with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                f.write(dedent(f'''\
builddir = .

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build {generated_abs}: phony
build build.ninja: verify
build out: touch ../srcroot/src/a.cpp gen/generated.cpp
default out
'''))

            self.assertEqual(
                self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)

            self._create_file_and_advance_dir_mtime(generated_dir, 'new.cpp')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(source_dir, 'new.cpp')
            fourth = self._run_ninja_in_dir(build_dir)
            self.assertIn('Re-checking...', fourth)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          fourth)
            self.assertIn('ninja: no work to do.', fourth)
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)

    def test_manifest_check_prunes_builddir_side_effect_dirs_out_of_source(
            self) -> None:
        # Out-of-source mixed sets can include build-local side-effect files in
        # directories where generators also declare outputs. Keep source-side
        # dirs and skip those generated-output directories.
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, 'srcroot')
            build_dir = os.path.join(root, 'build')
            os.mkdir(source_root)
            os.mkdir(build_dir)

            source_dir = os.path.join(source_root, 'src')
            os.mkdir(source_dir)
            with open(os.path.join(source_dir, 'a.cpp'), 'w'):
                pass

            generated_dir = os.path.join(build_dir, 'gen')
            generated_tmp_dir = os.path.join(generated_dir, 'tmp')
            os.mkdir(generated_dir)
            os.mkdir(generated_tmp_dir)
            with open(os.path.join(generated_tmp_dir, 'generated.cpp'), 'w'):
                pass

            with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                f.write(dedent('''\
builddir = .

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build gen/generated.h: phony
build build.ninja: verify
build out: touch ../srcroot/src/a.cpp gen/tmp/generated.cpp
default out
'''))

            self.assertEqual(
                self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)
            self.assertNotIn('inferred\tgen/tmp\n', cache_content)

            self._create_file_and_advance_dir_mtime(generated_tmp_dir, 'new.cpp')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(source_dir, 'new.cpp')
            fourth = self._run_ninja_in_dir(build_dir)
            self.assertIn('Re-checking...', fourth)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          fourth)
            self.assertIn('ninja: no work to do.', fourth)
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)
            self.assertNotIn('inferred\tgen/tmp\n', cache_content)

    def test_manifest_check_migrates_v2_cache_and_recomputes_inferred_dirs(
            self) -> None:
        # Schema upgrades must force inferred-dir recomputation so stale v2
        # build-local entries cannot keep triggering manifest checks.
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, 'srcroot')
            build_dir = os.path.join(root, 'build')
            os.mkdir(source_root)
            os.mkdir(build_dir)

            source_dir = os.path.join(source_root, 'src')
            os.mkdir(source_dir)
            with open(os.path.join(source_dir, 'a.cpp'), 'w'):
                pass

            generated_dir = os.path.join(build_dir, 'gen')
            os.mkdir(generated_dir)
            with open(os.path.join(generated_dir, 'generated.cpp'), 'w'):
                pass

            with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                f.write(dedent('''\
builddir = .

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build gen/generated.h: phony
build build.ninja: verify
build out: touch ../srcroot/src/a.cpp gen/generated.cpp
default out
'''))

            self.assertEqual(
                self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            manifest_path = os.path.join(build_dir, 'build.ninja')
            manifest_mtime = os.stat(manifest_path).st_mtime_ns
            generated_mtime = os.stat(generated_dir).st_mtime_ns
            cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
            with open(cache_path, 'w') as f:
                f.write('ninja_glob_dirs_v2\n')
                f.write(f'manifest\t{manifest_mtime}\tbuild.ninja\n')
                f.write('inferred\tgen\n')
                f.write(f'mtime\tgen\t{generated_mtime}\n')

            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('ninja_glob_dirs_v3\n', cache_content)
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)

            self._create_file_and_advance_dir_mtime(source_dir, 'new.cpp')
            third = self._run_ninja_in_dir(build_dir)
            self._assert_single_manifest_restart(third)

            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('ninja_glob_dirs_v3\n', cache_content)
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)

            self._create_file_and_advance_dir_mtime(generated_dir, 'new.cpp')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

    def test_manifest_check_migrates_v1_cache_and_recomputes_inferred_dirs(
            self) -> None:
        # Legacy v1 caches should be upgraded by recomputing inferred dirs.
        with tempfile.TemporaryDirectory() as root:
            source_root = os.path.join(root, 'srcroot')
            build_dir = os.path.join(root, 'build')
            os.mkdir(source_root)
            os.mkdir(build_dir)

            source_dir = os.path.join(source_root, 'src')
            os.mkdir(source_dir)
            with open(os.path.join(source_dir, 'a.cpp'), 'w'):
                pass

            generated_dir = os.path.join(build_dir, 'gen')
            os.mkdir(generated_dir)
            with open(os.path.join(generated_dir, 'generated.cpp'), 'w'):
                pass

            with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                f.write(dedent('''\
builddir = .

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build gen/generated.h: phony
build build.ninja: verify
build out: touch ../srcroot/src/a.cpp gen/generated.cpp
default out
'''))

            self.assertEqual(
                self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

            generated_mtime = os.stat(generated_dir).st_mtime_ns
            cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
            with open(cache_path, 'w') as f:
                f.write('ninja_glob_dirs_v1\n')
                f.write(f'gen\t{generated_mtime}\n')

            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertIn('ninja_glob_dirs_v3\n', cache_content)
            self.assertIn('inferred\t../srcroot/src\n', cache_content)
            self.assertNotIn('inferred\tgen\n', cache_content)

            self._create_file_and_advance_dir_mtime(source_dir, 'new.cpp')
            third = self._run_ninja_in_dir(build_dir)
            self._assert_single_manifest_restart(third)

            self._create_file_and_advance_dir_mtime(generated_dir, 'new.cpp')
            self.assertEqual(
                self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

    def test_manifest_check_preserves_future_cache_schema(self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch src/a.cpp
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            cache_path = os.path.join(b.path, '.ninja_glob_dirs')
            manifest_path = os.path.join(b.path, 'build.ninja')
            with open(cache_path, 'w') as f:
                f.write('ninja_glob_dirs_v999\n')
                f.write(f'manifest\t{os.stat(manifest_path).st_mtime_ns}\tbuild.ninja\n')
                f.write('inferred\tsrc\n')
                f.write(f'mtime\tsrc\t{os.stat(src_dir).st_mtime_ns}\n')

            with open(cache_path) as f:
                future_cache_content = f.read()

            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            compat_cache_path = cache_path + '.compat_v3'
            self.assertTrue(os.path.exists(compat_cache_path))
            with open(compat_cache_path) as f:
                compat_cache_content = f.read()
            self.assertTrue(compat_cache_content.startswith('ninja_glob_dirs_v3\n'))
            self.assertIn('inferred\tsrc\n', compat_cache_content)

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
            third = b.run(pipe=True)
            self._assert_single_manifest_restart(third)
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            with open(compat_cache_path) as f:
                updated_compat_cache_content = f.read()
            self.assertTrue(updated_compat_cache_content.startswith(
                'ninja_glob_dirs_v3\n'))
            self.assertIn('inferred\tsrc\n', updated_compat_cache_content)

            with open(cache_path) as f:
                cache_content = f.read()
            self.assertEqual(cache_content, future_cache_content)

    def test_manifest_check_keeps_source_dirs_with_generated_outputs_in_tree(
            self) -> None:
        # In-tree manifests can have generated outputs inside source
        # directories. Those source directories must stay watched.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build src/generated.h: phony
build build.ninja: verify
build out: touch src/a.cpp ${external_input}
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext:
                external_input = os.path.join(ext, 'input.txt')
                with open(external_input, 'w'):
                    pass

                with open(os.path.join(b.path, 'build.ninja'), 'r') as f:
                    plan = f.read()
                plan = plan.replace(
                    '${external_input}',
                    self._escape_ninja_path(external_input.replace('\\', '/')))
                with open(os.path.join(b.path, 'build.ninja'), 'w') as f:
                    f.write(plan)

                self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
                self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

                cache_path = os.path.join(b.path, '.ninja_glob_dirs')
                with open(cache_path) as f:
                    cache_content = f.read()
                self.assertIn('inferred\tsrc\n', cache_content)

                self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
                third = b.run(pipe=True)
                self._assert_single_manifest_restart(third)
                self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_keeps_source_dirs_with_generated_outputs_out_of_source(
            self) -> None:
        with tempfile.TemporaryDirectory() as root:
            build_dir = os.path.join(root, 'build')
            src_dir = os.path.join(build_dir, 'src')
            os.mkdir(build_dir)
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext:
                external_input = os.path.join(ext, 'input.txt')
                with open(external_input, 'w'):
                    pass

                with open(os.path.join(root, 'build.ninja'), 'w') as f:
                    f.write(dedent(f'''\
builddir = build

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build/src/generated.h: phony
build build.ninja: verify
build out: touch build/src/a.cpp {self._escape_ninja_path(external_input.replace('\\\\', '/'))}
default out
'''))

                self.assertEqual(
                    self._run_ninja_in_dir(root), '[1/1] touch out\n')
                self.assertEqual(
                    self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

                cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
                with open(cache_path) as f:
                    cache_content = f.read()
                self.assertIn('inferred\tbuild/src\n', cache_content)

                self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
                third = self._run_ninja_in_dir(root)
                self._assert_single_manifest_restart(third)
                self.assertEqual(
                    self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

    def test_manifest_check_keeps_in_tree_source_dirs_with_absolute_inputs(
            self) -> None:
        # Absolute in-tree source paths should remain watched even when there
        # are other absolute inputs outside the source tree.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build src/generated.h: phony
build build.ninja: verify
build out: touch ${source_input} ${external_input}
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            source_input = os.path.join(src_dir, 'a.cpp')
            with open(source_input, 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext:
                external_input = os.path.join(ext, 'input.txt')
                with open(external_input, 'w'):
                    pass

                with open(os.path.join(b.path, 'build.ninja'), 'r') as f:
                    plan = f.read()
                plan = plan.replace(
                    '${source_input}',
                    self._escape_ninja_path(source_input.replace('\\', '/')))
                plan = plan.replace(
                    '${external_input}',
                    self._escape_ninja_path(external_input.replace('\\', '/')))
                with open(os.path.join(b.path, 'build.ninja'), 'w') as f:
                    f.write(plan)

                self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
                self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

                cache_path = os.path.join(b.path, '.ninja_glob_dirs')
                with open(cache_path) as f:
                    cache_content = f.read()
                source_dir_entry = src_dir.replace('\\', '/')
                self.assertIn(f'inferred\t{source_dir_entry}\n', cache_content)

                self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
                third = b.run(pipe=True)
                self._assert_single_manifest_restart(third)
                self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_keeps_in_tree_source_dirs_with_multiple_absolute_inputs(
            self) -> None:
        # Multiple absolute external inputs should not cause in-tree source
        # directories to be pruned from inferred watches.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build src/generated.h: phony
build build.ninja: verify
build out: touch src/a.cpp ${external_input_a} ${external_input_b}
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext_a, tempfile.TemporaryDirectory() as ext_b:
                external_input_a = os.path.join(ext_a, 'a.txt')
                external_input_b = os.path.join(ext_b, 'b.txt')
                with open(external_input_a, 'w'):
                    pass
                with open(external_input_b, 'w'):
                    pass

                with open(os.path.join(b.path, 'build.ninja'), 'r') as f:
                    plan = f.read()
                plan = plan.replace(
                    '${external_input_a}',
                    self._escape_ninja_path(external_input_a.replace('\\', '/')))
                plan = plan.replace(
                    '${external_input_b}',
                    self._escape_ninja_path(external_input_b.replace('\\', '/')))
                with open(os.path.join(b.path, 'build.ninja'), 'w') as f:
                    f.write(plan)

                self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
                self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

                cache_path = os.path.join(b.path, '.ninja_glob_dirs')
                with open(cache_path) as f:
                    cache_content = f.read()
                self.assertIn('inferred\tsrc\n', cache_content)

                self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
                third = b.run(pipe=True)
                self._assert_single_manifest_restart(third)
                self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_ignores_cwd_equivalent_absolute_input_dirs(
            self) -> None:
        # Absolute paths under cwd should not infer cwd itself as a watched dir.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch ${absolute_input}
default out
''') as b:
            absolute_input = os.path.join(b.path, 'a.cpp')
            with open(absolute_input, 'w'):
                pass

            with open(os.path.join(b.path, 'build.ninja'), 'r') as f:
                plan = f.read()
            plan = plan.replace(
                '${absolute_input}',
                self._escape_ninja_path(absolute_input.replace('\\', '/')))
            with open(os.path.join(b.path, 'build.ninja'), 'w') as f:
                f.write(plan)

            self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            cache_path = os.path.join(b.path, '.ninja_glob_dirs')
            with open(cache_path) as f:
                cache_content = f.read()
            cwd_entry = b.path.replace('\\', '/')
            self.assertNotIn(f'inferred\t{cwd_entry}\n', cache_content)

            # A no-op run should stay clean and not enter manifest-check loops.
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_in_tree_with_absolute_manifest_path(self) -> None:
        # Absolute -f paths should not change inferred-watch semantics.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build src/generated.h: phony
build build.ninja: verify
build out: touch src/a.cpp ${external_input}
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext:
                external_input = os.path.join(ext, 'input.txt')
                with open(external_input, 'w'):
                    pass

                with open(os.path.join(b.path, 'build.ninja'), 'r') as f:
                    plan = f.read()
                plan = plan.replace(
                    '${external_input}',
                    self._escape_ninja_path(external_input.replace('\\', '/')))
                with open(os.path.join(b.path, 'build.ninja'), 'w') as f:
                    f.write(plan)

                abs_manifest = os.path.join(b.path, 'build.ninja')
                flags = f'-f {abs_manifest}'
                self.assertEqual(b.run(flags=flags, pipe=True), '[1/1] touch out\n')
                self.assertEqual(b.run(flags=flags, pipe=True), 'ninja: no work to do.\n')
                cache_path = os.path.join(b.path, '.ninja_glob_dirs')
                self.assertTrue(os.path.exists(cache_path))
                with open(cache_path) as f:
                    cache_content = f.read()
                self.assertIn('ninja_glob_dirs_v3\n', cache_content)
                self.assertIn('inferred\tsrc\n', cache_content)

                self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
                third = b.run(flags=flags, pipe=True)
                self._assert_single_manifest_restart(third)
                self.assertEqual(b.run(flags=flags, pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_with_absolute_symlink_manifest_path(self) -> None:
        # Absolute -f symlink aliases should still resolve manifest checks.
        with tempfile.TemporaryDirectory() as root:
            real_dir = os.path.join(root, 'real')
            alias_dir = os.path.join(root, 'alias')
            os.mkdir(real_dir)
            try:
                os.symlink(real_dir, alias_dir)
            except (OSError, NotImplementedError):
                self.skipTest('symlink creation is not available')

            src_dir = os.path.join(real_dir, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with open(os.path.join(real_dir, 'build.ninja'), 'w') as f:
                f.write(dedent('''\
rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch src/a.cpp
default out
'''))

            real_args = ['-f', os.path.join(real_dir, 'build.ninja')]
            self.assertEqual(
                self._run_ninja_in_dir(real_dir, real_args), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(real_dir, real_args),
                'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')

            alias_args = ['-f', os.path.join(alias_dir, 'build.ninja')]
            third = self._run_ninja_in_dir(real_dir, alias_args)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)

    def test_manifest_check_with_relative_symlink_manifest_path(self) -> None:
        # Relative -f symlink aliases should resolve to the same manifest node.
        with tempfile.TemporaryDirectory() as root:
            real_dir = os.path.join(root, 'real')
            alias_dir = os.path.join(root, 'alias')
            os.mkdir(real_dir)
            try:
                os.symlink(real_dir, alias_dir)
            except (OSError, NotImplementedError):
                self.skipTest('symlink creation is not available')

            src_dir = os.path.join(real_dir, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with open(os.path.join(real_dir, 'build.ninja'), 'w') as f:
                f.write(dedent('''\
rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch src/a.cpp
default out
'''))

            self.assertEqual(self._run_ninja_in_dir(real_dir), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(real_dir),
                'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')

            relative_alias_manifest = os.path.relpath(
                os.path.join(alias_dir, 'build.ninja'), real_dir)
            third = self._run_ninja_in_dir(
                real_dir, ['-f', relative_alias_manifest])
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)

    def test_manifest_check_unset_builddir_uses_manifest_directory(self) -> None:
        # When builddir is unset and Ninja is invoked with -f build/build.ninja,
        # treat the manifest directory as the effective build root.
        with tempfile.TemporaryDirectory() as root:
            source_dir = os.path.join(root, 'src')
            build_dir = os.path.join(root, 'build')
            os.mkdir(source_dir)
            os.mkdir(build_dir)
            with open(os.path.join(source_dir, 'a.cpp'), 'w'):
                pass

            generated_dir = os.path.join(build_dir, 'gen')
            os.mkdir(generated_dir)
            with open(os.path.join(generated_dir, 'generated.cpp'), 'w'):
                pass

            with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                f.write(dedent('''\
rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build/gen/generated.h: phony
build build/build.ninja: verify
build out: touch src/a.cpp build/gen/generated.cpp
default out
'''))

            args = ['-f', 'build/build.ninja']
            self.assertEqual(
                self._run_ninja_in_dir(root, args), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(root, args), 'ninja: no work to do.\n')
            self.assertFalse(os.path.exists(os.path.join(root, '.ninja_glob_dirs')))
            self.assertTrue(os.path.exists(os.path.join(build_dir, '.ninja_glob_dirs')))

            self._create_file_and_advance_dir_mtime(generated_dir, 'new.cpp')
            self.assertEqual(
                self._run_ninja_in_dir(root, args), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(source_dir, 'new.cpp')
            fourth = self._run_ninja_in_dir(root, args)
            self.assertIn('Re-checking...', fourth)
            self.assertIn(
                'regeneration complete; restarting with updated manifest...',
                fourth)
            self.assertIn('ninja: no work to do.', fourth)
            self.assertEqual(
                self._run_ninja_in_dir(root, args), 'ninja: no work to do.\n')

    def test_manifest_check_on_non_source_suffix_input_change(self) -> None:
        # Plain glob-like manifests can include arbitrary file extensions, not
        # just C/C++ sources.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule copy
  command = touch $out
  description = COPY $out

build build.ninja: verify
build out: copy assets/data.txt
default out
''') as b:
            assets_dir = os.path.join(b.path, 'assets')
            os.mkdir(assets_dir)
            with open(os.path.join(assets_dir, 'data.txt'), 'w'):
                pass

            self.assertEqual(b.run(pipe=True), '[1/1] COPY out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(
                assets_dir, 'new.json')

            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn('COPY out', third)
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_on_template_so_in_input_change(self) -> None:
        # Template inputs like *.so.in are source-like and should stay watched.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule copy
  command = touch $out
  description = COPY $out

build build.ninja: verify
build out: copy src/template.so.in
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'template.so.in'), 'w'):
                pass

            self.assertEqual(b.run(pipe=True), '[1/1] COPY out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_dir, 'new_template.in')

            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn('COPY out', third)

    def test_manifest_check_ignores_versioned_shared_object_inputs(self) -> None:
        # Versioned shared objects are binary artifacts and should be skipped.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule copy
  command = touch $out
  description = COPY $out

build build.ninja: verify
build out: copy lib/libfoo.so.1
default out
''') as b:
            lib_dir = os.path.join(b.path, 'lib')
            os.mkdir(lib_dir)
            with open(os.path.join(lib_dir, 'libfoo.so.1'), 'w'):
                pass

            self.assertEqual(b.run(pipe=True), '[1/1] COPY out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            cache_path = os.path.join(b.path, '.ninja_glob_dirs')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertNotIn('inferred\tlib\n', cache_content)

            self._create_file_and_advance_dir_mtime(lib_dir, 'newlib.so.2')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

    def test_manifest_check_no_regen_loop_when_regen_touches_build_local_dir(
            self) -> None:
        # If a manifest regeneration run mutates a watched build-local
        # directory, Ninja should still perform at most one manifest restart.
        with tempfile.TemporaryDirectory() as root:
            build_dir = os.path.join(root, 'build')
            os.mkdir(build_dir)
            generated_dir = os.path.join(build_dir, 'gen')
            os.mkdir(generated_dir)
            with open(os.path.join(generated_dir, 'generated.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext:
                external_input = os.path.join(ext, 'input.txt')
                with open(external_input, 'w'):
                    pass

                py = self._escape_ninja_path(sys.executable.replace('\\', '/'))
                ext_input = self._escape_ninja_path(
                    external_input.replace('\\', '/'))
                with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                    f.write(dedent(f'''\
builddir = .

rule verify
  command = touch $out && {py} -c "import pathlib,time; d = pathlib.Path('gen'); d.mkdir(exist_ok=True); (d / ('stamp_' + str(time.time_ns()))).write_text('')"
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build gen/generated.h: phony
build build.ninja: verify
build out: touch gen/generated.cpp {ext_input}
default out
'''))

                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

                self._create_file_and_advance_dir_mtime(ext, 'new.cpp')
                third = self._run_ninja_in_dir(build_dir)
                self._assert_single_manifest_restart(third)
                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

    def test_manifest_check_with_glob_watchfile(self) -> None:
        # A generator can explicitly declare watched directories through
        # glob_watchfile bindings.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build out: touch
default out
''') as b:
            watched = os.path.join(b.path, 'watched')
            os.mkdir(watched)
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('watched\n')

            self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(watched, 'entry.txt')

            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn('touch out', third)

    def test_manifest_check_with_glob_watchfile_prefers_explicit_dirs(self) -> None:
        # If glob_watchfile is set, its entries are authoritative and inferred
        # source-directory watching is skipped.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule cc
  command = touch $out
  description = CXX $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build a.o: cc src/a.cpp
default a.o
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            watched = os.path.join(b.path, 'watched')
            os.mkdir(watched)
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('watched\n')

            self.assertEqual(b.run(pipe=True), '[1/1] CXX a.o\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            # Source dir changes should not trigger manifest checks when
            # glob_watchfile is present.
            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')

            third = b.run(pipe=True)
            self.assertEqual(third, 'ninja: no work to do.\n')

            # Explicitly watched dirs still trigger.
            self._create_file_and_advance_dir_mtime(watched, 'entry.txt')

            fourth = b.run(pipe=True)
            self.assertIn('Re-checking...', fourth)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          fourth)
            self.assertIn('ninja: no work to do.', fourth)
            self.assertNotIn('CXX a.o', fourth)

    def test_manifest_check_with_glob_watchfile_ignores_dot_entry(self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule cc
  command = touch $out
  description = CXX $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build a.o: cc src/a.cpp
default a.o
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            watched = os.path.join(b.path, 'watched')
            os.mkdir(watched)
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('.\n')
                f.write('watched\n')

            self.assertEqual(b.run(pipe=True), '[1/1] CXX a.o\n')

            cache_path = os.path.join(b.path, '.ninja_glob_dirs')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertNotIn('mtime\t.\t', cache_content)
            self.assertIn('mtime\twatched\t', cache_content)

            self._create_file_and_advance_dir_mtime(b.path, 'watch_trigger.txt')
            third = b.run(pipe=True)
            self.assertEqual(third, 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(watched, 'entry.txt')
            fourth = b.run(pipe=True)
            self.assertIn('Re-checking...', fourth)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          fourth)
            self.assertIn('ninja: no work to do.', fourth)

    def test_manifest_check_with_glob_watchfile_ignores_cwd_absolute_entry(
            self) -> None:
        # Absolute cwd aliases in glob_watchfile are as noisy as "." and must
        # be ignored.
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule cc
  command = touch $out
  description = CXX $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build a.o: cc src/a.cpp
default a.o
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            watched = os.path.join(b.path, 'watched')
            os.mkdir(watched)
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write(f'{b.path.replace("\\\\", "/")}\n')
                f.write('watched\n')

            self.assertEqual(b.run(pipe=True), '[1/1] CXX a.o\n')

            cache_path = os.path.join(b.path, '.ninja_glob_dirs')
            with open(cache_path) as f:
                cache_content = f.read()
            self.assertNotIn(f'mtime\t{b.path.replace("\\\\", "/")}\t',
                             cache_content)
            self.assertIn('mtime\twatched\t', cache_content)

            self._create_file_and_advance_dir_mtime(b.path, 'watch_trigger.txt')
            third = b.run(pipe=True)
            self.assertEqual(third, 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(watched, 'entry.txt')
            fourth = b.run(pipe=True)
            self.assertIn('Re-checking...', fourth)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          fourth)
            self.assertIn('ninja: no work to do.', fourth)

    def test_manifest_check_post_regen_ignores_removed_glob_watchfile(
            self) -> None:
        # If regeneration removes glob_watchfile, post-regeneration cache
        # refresh must not use stale watchfile bindings from the old graph.
        with tempfile.TemporaryDirectory() as root:
            py = self._escape_ninja_path(sys.executable.replace('\\', '/'))
            watched = os.path.join(root, 'watched')
            os.mkdir(watched)
            with open(os.path.join(root, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('watched\n')

            with open(os.path.join(root, 'touch.py'), 'w') as f:
                f.write('import pathlib,sys\n')
                f.write('pathlib.Path(sys.argv[1]).touch()\n')

            with open(os.path.join(root, 'regen.py'), 'w') as f:
                f.write(dedent(f"""\
import pathlib
import textwrap

root = pathlib.Path(__file__).resolve().parent
(root / 'build.ninja').write_text(textwrap.dedent('''\
rule verify
  command = {py} regen.py
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = {py} touch.py $out
  description = touch $out

build build.ninja: verify
build out: touch
default out
'''))
watchfile = root / 'watch_dirs.txt'
if watchfile.exists():
    watchfile.unlink()
"""))

            with open(os.path.join(root, 'build.ninja'), 'w') as f:
                f.write(dedent(f'''\
rule verify
  command = {py} regen.py
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = {py} touch.py $out
  description = touch $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build out: touch
default out
'''))

            self.assertEqual(self._run_ninja_in_dir(root), '[1/1] touch out\n')
            self.assertEqual(self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(watched, 'new.txt')
            third = self._run_ninja_in_dir(root)
            self._assert_single_manifest_restart(third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn("glob watch file 'watch_dirs.txt' not found", third)
            self.assertEqual(
                self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

    def test_manifest_check_post_regen_recomputes_inferred_dirs(
            self) -> None:
        # If regeneration changes source inputs from src_old to src_new, the
        # next manifest check must watch src_new (not stale src_old cache).
        with tempfile.TemporaryDirectory() as root:
            py = self._escape_ninja_path(sys.executable.replace('\\', '/'))
            src_old = os.path.join(root, 'src_old')
            src_new = os.path.join(root, 'src_new')
            os.mkdir(src_old)
            os.mkdir(src_new)
            with open(os.path.join(src_old, 'a.cpp'), 'w'):
                pass
            with open(os.path.join(src_new, 'a.cpp'), 'w'):
                pass

            with open(os.path.join(root, 'regen.py'), 'w') as f:
                f.write(dedent(f"""\
import pathlib
import textwrap

root = pathlib.Path(__file__).resolve().parent
(root / 'build.ninja').write_text(textwrap.dedent('''\
rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch src_new/a.cpp
default out
'''))
"""))

            with open(os.path.join(root, 'build.ninja'), 'w') as f:
                f.write(dedent(f'''\
rule verify
  command = {py} regen.py
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch src_old/a.cpp
default out
'''))

            self.assertEqual(self._run_ninja_in_dir(root), '[1/1] touch out\n')
            self.assertEqual(self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_old, 'trigger.cpp')
            third = self._run_ninja_in_dir(root)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)

            self._create_file_and_advance_dir_mtime(src_new, 'trigger.cpp')
            fourth = self._run_ninja_in_dir(root)
            self.assertIn('Re-checking...', fourth)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          fourth)
            self.assertIn('ninja: no work to do.', fourth)

            self.assertEqual(self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

    def test_manifest_check_explicit_builddir_keeps_generated_dirs_with_multiple_absolutes(
            self) -> None:
        # In ambiguous mixed-absolute layouts, prefer keeping build-local dirs
        # watched to avoid false negatives on real source-directory changes.
        with tempfile.TemporaryDirectory() as root:
            build_dir = os.path.join(root, 'build')
            generated_dir = os.path.join(build_dir, 'gen')
            os.mkdir(build_dir)
            os.mkdir(generated_dir)
            with open(os.path.join(generated_dir, 'generated.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext_a, tempfile.TemporaryDirectory() as ext_b:
                ext_a_dir = os.path.join(ext_a, 'src')
                ext_b_dir = os.path.join(ext_b, 'src')
                os.mkdir(ext_a_dir)
                os.mkdir(ext_b_dir)
                ext_a_input = os.path.join(ext_a_dir, 'a.cpp')
                ext_b_input = os.path.join(ext_b_dir, 'b.cpp')
                with open(ext_a_input, 'w'):
                    pass
                with open(ext_b_input, 'w'):
                    pass

                with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                    f.write(dedent(f'''\
builddir = .

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build gen/generated.h: phony
build build.ninja: verify
build out: touch gen/generated.cpp {self._escape_ninja_path(ext_a_input.replace('\\\\', '/'))} {self._escape_ninja_path(ext_b_input.replace('\\\\', '/'))}
default out
'''))

                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

                cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
                with open(cache_path) as f:
                    cache_content = f.read()
                self.assertIn('inferred\tgen\n', cache_content)

                self._create_file_and_advance_dir_mtime(generated_dir, 'new.cpp')
                third = self._run_ninja_in_dir(build_dir)
                self.assertIn('Re-checking...', third)
                self.assertIn('regeneration complete; restarting with updated manifest...',
                              third)
                self.assertIn('ninja: no work to do.', third)

                self._create_file_and_advance_dir_mtime(ext_a_dir, 'new.cpp')
                fourth = self._run_ninja_in_dir(build_dir)
                self.assertIn('Re-checking...', fourth)
                self.assertIn('regeneration complete; restarting with updated manifest...',
                              fourth)
                self.assertIn('ninja: no work to do.', fourth)

    def test_manifest_check_failed_regeneration_does_not_consume_watch_change(
            self) -> None:
        # A failed manifest check must not acknowledge the watched-dir mtime
        # change permanently; the next run should still attempt a re-check.
        with BuildDir('''rule verify
  command = false
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch src/a.cpp
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')

            first = subprocess.run(
                [NINJA_PATH],
                cwd=b.path,
                env=default_env,
                capture_output=True,
                check=False,
                text=True)
            first_output = first.stdout + first.stderr
            self.assertEqual(first.returncode, 1)
            self.assertIn('Re-checking...', first_output)
            self.assertIn("ninja: error: rebuilding 'build.ninja': subcommand failed",
                          first_output)

            second = subprocess.run(
                [NINJA_PATH],
                cwd=b.path,
                env=default_env,
                capture_output=True,
                check=False,
                text=True)
            second_output = second.stdout + second.stderr
            self.assertEqual(second.returncode, 1)
            self.assertIn('Re-checking...', second_output)
            self.assertIn("ninja: error: rebuilding 'build.ninja': subcommand failed",
                          second_output)

    def test_manifest_check_explicit_builddir_keeps_source_dirs_with_generated_outputs_and_multiple_absolutes(
            self) -> None:
        # Explicit builddir with multiple absolute inputs should still keep
        # real source directories watched even if generated outputs share them.
        with tempfile.TemporaryDirectory() as root:
            build_dir = os.path.join(root, 'build')
            src_dir = os.path.join(build_dir, 'src')
            os.mkdir(build_dir)
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            with tempfile.TemporaryDirectory() as ext_a, tempfile.TemporaryDirectory() as ext_b:
                ext_a_dir = os.path.join(ext_a, 'src')
                ext_b_dir = os.path.join(ext_b, 'src')
                os.mkdir(ext_a_dir)
                os.mkdir(ext_b_dir)
                ext_a_input = os.path.join(ext_a_dir, 'a.cpp')
                ext_b_input = os.path.join(ext_b_dir, 'b.cpp')
                with open(ext_a_input, 'w'):
                    pass
                with open(ext_b_input, 'w'):
                    pass

                with open(os.path.join(build_dir, 'build.ninja'), 'w') as f:
                    f.write(dedent(f'''\
builddir = .

rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build src/generated.h: phony
build build.ninja: verify
build out: touch src/a.cpp {self._escape_ninja_path(ext_a_input.replace('\\\\', '/'))} {self._escape_ninja_path(ext_b_input.replace('\\\\', '/'))}
default out
'''))

                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), '[1/1] touch out\n')
                self.assertEqual(
                    self._run_ninja_in_dir(build_dir), 'ninja: no work to do.\n')

                cache_path = os.path.join(build_dir, '.ninja_glob_dirs')
                with open(cache_path) as f:
                    cache_content = f.read()
                self.assertIn('inferred\tsrc\n', cache_content)

                self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
                third = self._run_ninja_in_dir(build_dir)
                self.assertIn('Re-checking...', third)
                self.assertIn('regeneration complete; restarting with updated manifest...',
                              third)
                self.assertIn('ninja: no work to do.', third)

    def test_manifest_check_missing_glob_watchfile(self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  restat = 1
  generator = 1

build build.ninja: verify
  glob_watchfile = missing_watch_dirs.txt
''') as b:
            proc = subprocess.run(
                [NINJA_PATH],
                cwd=b.path,
                env=default_env,
                capture_output=True,
                check=False,
                text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, '')
            self.assertEqual(
                proc.stderr,
                "ninja: error: rebuilding 'build.ninja': "
                "glob watch file 'missing_watch_dirs.txt' not found\n")

    def test_manifest_check_honors_absolute_watch_dirs_under_builddir(
            self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  restat = 1
  generator = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build out: touch src/a.cpp
default out
''') as b:
            src_dir = os.path.join(b.path, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass

            ignored = os.path.join(b.path, 'ignored')
            os.mkdir(ignored)
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write(os.path.join(b.path, 'ignored') + '\n')

            self.assertEqual(b.run(pipe=True), '[1/1] touch out\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(ignored, 'entry.txt')
            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn('touch out', third)

    def test_manifest_check_errors_on_unknown_glob_watchfile_schema(self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  restat = 1
  generator = 1

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
''') as b:
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v2\n')
                f.write('watched\n')

            proc = subprocess.run(
                [NINJA_PATH],
                cwd=b.path,
                env=default_env,
                capture_output=True,
                check=False,
                text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, '')
            self.assertEqual(
                proc.stderr,
                "ninja: error: rebuilding 'build.ninja': "
                "parsing glob watch file 'watch_dirs.txt': "
                "unsupported glob watch file schema "
                "'ninja_glob_watch_dirs_v2'\n")

    def test_manifest_check_unreadable_glob_watchfile(self) -> None:
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('root can bypass unreadable file permissions')
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  restat = 1
  generator = 1

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
''') as b:
            watchfile = os.path.join(b.path, 'watch_dirs.txt')
            with open(watchfile, 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('watched\n')
            os.chmod(watchfile, 0)

            proc = subprocess.run(
                [NINJA_PATH],
                cwd=b.path,
                env=default_env,
                capture_output=True,
                check=False,
                text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, '')
            self.assertIn("ninja: error: rebuilding 'build.ninja': ", proc.stderr)
            self.assertIn("loading glob watch file 'watch_dirs.txt': ", proc.stderr)

    def test_manifest_check_when_watched_directory_disappears(self) -> None:
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  restat = 1
  generator = 1

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
''') as b:
            watched = os.path.join(b.path, 'watched')
            os.mkdir(watched)
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('watched\n')

            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')
            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            os.rmdir(watched)
            third = b.run(pipe=True)
            self.assertIn('Re-checking...', third)
            self.assertIn('regeneration complete; restarting with updated manifest...',
                          third)
            self.assertIn('ninja: no work to do.', third)

    def test_manifest_check_error_when_watched_directory_stat_fails(self) -> None:
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('root can bypass directory permission checks')
        with BuildDir('''rule verify
  command = printf ""
  description = Re-checking...
  restat = 1
  generator = 1

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
''') as b:
            denied_parent = os.path.join(b.path, 'denied')
            os.mkdir(denied_parent)
            os.mkdir(os.path.join(denied_parent, 'sub'))
            with open(os.path.join(b.path, 'watch_dirs.txt'), 'w') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('denied/sub\n')

            self.assertEqual(b.run(pipe=True), 'ninja: no work to do.\n')

            try:
                os.chmod(denied_parent, 0)
                proc = subprocess.run(
                    [NINJA_PATH],
                    cwd=b.path,
                    env=default_env,
                    capture_output=True,
                    check=False,
                    text=True)
            finally:
                os.chmod(denied_parent, 0o700)

            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, '')
            self.assertIn(
                "ninja: error: rebuilding 'build.ninja': stat(denied/sub): ",
                proc.stderr)

    def test_phase_marker_absent_without_manifest_phase(self) -> None:
        # If there is no manifest rebuild/check work, no phase boundary marker
        # should be emitted.
        self.assertEqual(run(
'''rule touch
  command = touch $out
  description = touch $out

build out: touch
default out
''', pipe=True),
'''[1/1] touch out
''')

    def test_regeneration_phase_marker_after_manifest_restart(self) -> None:
        # If manifest regeneration updated build.ninja and triggers a restart,
        # print an explicit phase boundary before building user targets.
        self.assertEqual(run(
'''rule regen_once
  command = [ -f .regen_done ] || (touch $out && touch .regen_done)
  description = Regenerating...
  restat = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: regen_once
build out: touch
default out
''', pipe=True),
'''[1/1] Regenerating...
ninja: regeneration complete; restarting with updated manifest...
[1/1] touch out
''')

    def test_regeneration_then_manifest_check_phase_markers(self) -> None:
        # If a regeneration restart is followed by a check-only manifest phase,
        # print an explicit phase boundary before each distinct phase.
        with BuildDir('''rule regen_once
  command = [ -f .regen_done ] || (cp build.ninja.next $out && touch .regen_done)
  description = Re-running CMake...
  restat = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: regen_once
build out: touch
default out
''') as b:
            with open(os.path.join(b.path, 'build.ninja.next'), 'w') as f:
                f.write(dedent('''\
rule verify
  command = printf ""
  description = Re-checking...
  pool = console
  restat = 1

rule touch
  command = touch $out
  description = touch $out

build build.ninja: verify
build out: touch
default out
'''))

            self.assertEqual(b.run(pipe=True),
'''[1/1] Re-running CMake...
ninja: regeneration complete; restarting with updated manifest...
[1/1] Re-checking...
ninja: manifest check complete; building requested targets...
[1/1] touch out
''')

    def test_pr_1685(self) -> None:
        # Running those tools without .ninja_deps and .ninja_log shouldn't fail.
        self.assertEqual(run('', flags='-t recompact'), '')
        self.assertEqual(run('', flags='-t restat'), '')

    def test_issue_2048(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'build.ninja'), 'w'):
                pass

            with open(os.path.join(d, '.ninja_log'), 'w') as f:
                f.write('# ninja log v4\n')

            try:
                output = subprocess.check_output([NINJA_PATH, '-t', 'recompact'],
                                                 cwd=d,
                                                 env=default_env,
                                                 stderr=subprocess.STDOUT,
                                                 text=True
                                                 )

                self.assertEqual(
                    output.strip(),
                    "ninja: warning: build log version is too old; starting over"
                )
            except subprocess.CalledProcessError as err:
                self.fail("non-zero exit code with: " + err.output)

    def test_pr_2540(self)->None:
        py = sys.executable
        plan = f'''\
rule CUSTOM_COMMAND
  command = $COMMAND

build 124: CUSTOM_COMMAND
  COMMAND = {py} -c 'exit(124)'

build 127: CUSTOM_COMMAND
  COMMAND = {py} -c 'exit(127)'

build 130: CUSTOM_COMMAND
  COMMAND = {py} -c 'exit(130)'

build 137: CUSTOM_COMMAND
  COMMAND = {py} -c 'exit(137)'

build success: CUSTOM_COMMAND
  COMMAND = sleep 0.3; echo success
'''
        # Disable colors
        env = default_env.copy()
        env['TERM'] = 'dumb'
        self._test_expected_error(
            plan, '124',
            f'''[1/1] {py} -c 'exit(124)'
FAILED: [code=124] 124 \n{py} -c 'exit(124)'
ninja: build stopped: subcommand failed.
''',
            exit_code=124, env=env,
        )
        self._test_expected_error(
            plan, '127',
            f'''[1/1] {py} -c 'exit(127)'
FAILED: [code=127] 127 \n{py} -c 'exit(127)'
ninja: build stopped: subcommand failed.
''',
            exit_code=127, env=env,
        )
        self._test_expected_error(
            plan, '130',
            'ninja: build stopped: interrupted by user.\n',
            exit_code=130, env=env,
        )
        self._test_expected_error(
            plan, '137',
            f'''[1/1] {py} -c 'exit(137)'
FAILED: [code=137] 137 \n{py} -c 'exit(137)'
ninja: build stopped: subcommand failed.
''',
            exit_code=137, env=env,
        )
        self._test_expected_error(
            plan, 'non-existent-target',
            "ninja: error: unknown target 'non-existent-target'\n",
            exit_code=1, env=env,
        )
        self._test_expected_error(
            plan, '-j2 success 127',
            f'''[1/2] {py} -c 'exit(127)'
FAILED: [code=127] 127 \n{py} -c 'exit(127)'
[2/2] sleep 0.3; echo success
success
ninja: build stopped: subcommand failed.
''',
            exit_code=127, env=env,
        )

    def test_depfile_directory_creation(self) -> None:
        b = BuildDir('''\
            rule touch
              command = touch $out && echo "$out: extra" > $depfile

            build somewhere/out: touch
              depfile = somewhere_else/out.d
            ''')
        with b:
            self.assertEqual(b.run('', pipe=True), dedent('''\
                [1/1] touch somewhere/out && echo "somewhere/out: extra" > somewhere_else/out.d
                '''))
            self.assertTrue(os.path.isfile(os.path.join(b.d.name, "somewhere", "out")))
            self.assertTrue(os.path.isfile(os.path.join(b.d.name, "somewhere_else", "out.d")))

    def test_status(self) -> None:
        self.assertEqual(run(''), 'ninja: no work to do.\n')
        self.assertEqual(run('', pipe=True), 'ninja: no work to do.\n')
        self.assertEqual(run('', flags='--quiet'), '')

    def test_ninja_status_default(self) -> None:
        'Do we show the default status by default?'
        self.assertEqual(run(Output.BUILD_SIMPLE_ECHO), '[1/1] echo a\x1b[K\ndo thing\n')

    def test_ninja_status_quiet(self) -> None:
        'Do we suppress the status information when --quiet is specified?'
        output = run(Output.BUILD_SIMPLE_ECHO, flags='--quiet')
        self.assertEqual(output, 'do thing\n')

    def test_entering_directory_on_stdout(self) -> None:
        output = run(Output.BUILD_SIMPLE_ECHO, flags='-C$PWD', pipe=True)
        self.assertEqual(output.splitlines()[0][:25], "ninja: Entering directory")

    def test_tool_inputs(self) -> None:
        plan = '''
rule cat
  command = cat $in $out
build out1 : cat in1
build out2 : cat in2 out1
build out3 : cat out2 out1 | implicit || order_only
'''
        self.assertEqual(run(plan, flags='-t inputs out3'),
'''implicit
in1
in2
order_only
out1
out2
''')

        self.assertEqual(run(plan, flags='-t inputs --dependency-order out3'),
'''in2
in1
out1
out2
implicit
order_only
''')

        # Verify that results are shell-escaped by default, unless --no-shell-escape
        # is used. Also verify that phony outputs are never part of the results.
        quote = '"' if platform.system() == "Windows" else "'"

        plan = '''
rule cat
  command = cat $in $out
build out1 : cat in1
build out$ 2 : cat out1
build out$ 3 : phony out$ 2
build all: phony out$ 3
'''

        # Quoting changes the order of results when sorting alphabetically.
        self.assertEqual(run(plan, flags='-t inputs all'),
f'''{quote}out 2{quote}
in1
out1
''')

        self.assertEqual(run(plan, flags='-t inputs --no-shell-escape all'),
'''in1
out 2
out1
''')

        # But not when doing dependency order.
        self.assertEqual(
            run(
              plan,
              flags='-t inputs --dependency-order all'
            ),
            f'''in1
out1
{quote}out 2{quote}
''')

        self.assertEqual(
          run(
            plan,
            flags='-t inputs --dependency-order --no-shell-escape all'
          ),
          f'''in1
out1
out 2
''')

        self.assertEqual(
          run(
            plan,
            flags='-t inputs --dependency-order --no-shell-escape --print0 all'
          ),
          f'''in1\0out1\0out 2\0'''
        )


    def test_tool_compdb_targets(self) -> None:
        plan = '''
rule cat
  command = cat $in $out
build out1 : cat in1
build out2 : cat in2 out1
build out3 : cat out2 out1
build out4 : cat in4
'''


        self._test_expected_error(plan, '-t compdb-targets',
'''ninja: error: compdb-targets expects the name of at least one target
usage: ninja -t compdb [-hx] target [targets]

options:
  -h     display this help message
  -x     expand @rspfile style response file invocations
''')

        self._test_expected_error(plan, '-t compdb-targets in1',
            "ninja: fatal: 'in1' is not a target (i.e. it is not an output of any `build` statement)\n")

        self._test_expected_error(plan, '-t compdb-targets nonexistent_target',
            "ninja: fatal: unknown target 'nonexistent_target'\n")


        with BuildDir(plan) as b:
            actual = b.run(flags='-t compdb-targets out3')
            expected = f'''[
  {{
    "directory": "{b.path}",
    "command": "cat in1 out1",
    "file": "in1",
    "output": "out1"
  }},
  {{
    "directory": "{b.path}",
    "command": "cat in2 out1 out2",
    "file": "in2",
    "output": "out2"
  }},
  {{
    "directory": "{b.path}",
    "command": "cat in2 out1 out2",
    "file": "out1",
    "output": "out2"
  }},
  {{
    "directory": "{b.path}",
    "command": "cat out2 out1 out3",
    "file": "out2",
    "output": "out3"
  }},
  {{
    "directory": "{b.path}",
    "command": "cat out2 out1 out3",
    "file": "out1",
    "output": "out3"
  }}
]
'''
            self.assertEqual(expected, actual)


    def test_tool_multi_inputs(self) -> None:
        plan = '''
rule cat
  command = cat $in $out
build out1 : cat in1
build out2 : cat in1 in2
build out3 : cat in1 in2 in3
'''
        self.assertEqual(run(plan, flags='-t multi-inputs out1'),
'''out1<TAB>in1
'''.replace("<TAB>", "\t"))

        self.assertEqual(run(plan, flags='-t multi-inputs out1 out2 out3'),
'''out1<TAB>in1
out2<TAB>in1
out2<TAB>in2
out3<TAB>in1
out3<TAB>in2
out3<TAB>in3
'''.replace("<TAB>", "\t"))

        self.assertEqual(run(plan, flags='-t multi-inputs -d: out1'),
'''out1:in1
''')

        self.assertEqual(
          run(
            plan,
            flags='-t multi-inputs -d, --print0 out1 out2'
          ),
          '''out1,in1\0out2,in1\0out2,in2\0'''
        )


    def test_explain_output(self):
        b = BuildDir('''\
            build .FORCE: phony
            rule create_if_non_exist
              command = [ -e $out ] || touch $out
              restat = true
            rule write
              command = cp $in $out
            build input : create_if_non_exist .FORCE
            build mid : write input
            build output : write mid
            default output
            ''')
        with b:
            # The explain output is shown just before the relevant build:
            self.assertEqual(b.run('-v -d explain'), dedent('''\
                ninja explain: .FORCE is dirty
                [1/3] [ -e input ] || touch input
                ninja explain: input is dirty
                [2/3] cp input mid
                ninja explain: mid is dirty
                [3/3] cp mid output
                '''))
            # Don't print "ninja explain: XXX is dirty" for inputs that are
            # pruned from the graph by an earlier restat.
            self.assertEqual(b.run('-v -d explain'), dedent('''\
                ninja explain: .FORCE is dirty
                [1/1] [ -e input ] || touch input
                '''))

    def test_restat_prunes_progress_total(self):
        b = BuildDir('''\
            build .FORCE: phony
            rule create_if_non_exist
              command = [ -e $out ] || touch $out
              restat = true
            rule write
              command = cp $in $out
            build input : create_if_non_exist .FORCE
            build mid : write input
            build output : write mid
            default output
            ''')
        with b:
            # First run builds the full chain.
            self.assertEqual(b.run('-v'), dedent('''\
                [1/3] [ -e input ] || touch input
                [2/3] cp input mid
                [3/3] cp mid output
                '''))
            # On the no-op run, restat pruning should update the denominator
            # before status is printed.
            self.assertEqual(b.run('-v'), dedent('''\
                [1/1] [ -e input ] || touch input
                '''))

    def test_generator_restat_prunes_progress_total(self):
        b = BuildDir('''\
            build .FORCE: phony
            rule verify
              command = [ -e $out ] || touch $out
              restat = true
              generator = true
            rule write
              command = cp $in $out
            build input : verify .FORCE
            build mid : write input
            build output : write mid
            default output
            ''')
        with b:
            # First run builds the full chain.
            self.assertEqual(b.run('-v'), dedent('''\
                [1/3] [ -e input ] || touch input
                [2/3] cp input mid
                [3/3] cp mid output
                '''))
            # On the no-op run, generator+restat pruning should update the
            # denominator before status is printed.
            self.assertEqual(b.run('-v'), dedent('''\
                [1/1] [ -e input ] || touch input
                '''))

    def test_issue_2586(self):
        """This shouldn't hang"""
        plan = '''rule echo
  command = echo echo
build dep: echo
build console1: echo dep
  pool = console
build console2: echo
  pool = console
build all: phony console1 console2
default all
'''
        self.assertEqual(run(plan, flags='-j2', env={'NINJA_STATUS':''}), '''echo echo
echo
echo echo
echo
echo echo
echo
''')

    def test_issue_2621(self):
        """Should result in "multiple rules generate" error"""
        plan = r"""rule dd
  command = printf 'ninja_dyndep_version = 1\nbuild stamp-$n | out: dyndep\n' > $out
rule touch
  command = touch stamp-$n out
  dyndep = dd-$n
build dd-1: dd
  n = 1
build dd-2: dd
  n = 2
build stamp-1: touch || dd-1
  n = 1
build stamp-2: touch || dd-2
  n = 2
"""
        actual = ''
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            run(plan, '-v', print_err_output=False)
        actual = cm.exception.cooked_output
        self.assertEqual(cm.exception.returncode, 1)
        # dd-1 and dd-2 are both ready initially; scheduler order is not stable.
        self.assertIn(
            r"printf 'ninja_dyndep_version = 1\nbuild stamp-1 | out: dyndep\n' > dd-1",
            actual)
        self.assertIn(
            r"printf 'ninja_dyndep_version = 1\nbuild stamp-2 | out: dyndep\n' > dd-2",
            actual)
        # The conflict must be detected before any touch command runs.
        self.assertNotIn('touch stamp-1 out', actual)
        self.assertNotIn('touch stamp-2 out', actual)
        self.assertTrue(
            actual.endswith(
                "ninja: build stopped: multiple rules generate out.\n"))

    def test_issue_2681(self):
        """Ninja should return a status code of 130 when interrupted."""
        plan = r"""rule sleep
  command = sleep 10

build foo: sleep
"""
        with BuildDir(plan) as b:
            for signum in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
                proc = subprocess.Popen([NINJA_PATH, "foo"], cwd=b.path, env=default_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                # Sleep a bit to let Ninja start the build, otherwise the signal could be received
                # before it, and returncode will be -2.
                time.sleep(0.2)
                os.kill(proc.pid, signum)
                proc.wait()
                self.assertEqual(proc.returncode, 130, msg=f"For signal {signum}")


@unittest.skipUnless(platform.system() == 'Windows', 'Windows-only tests')
class WindowsOutput(unittest.TestCase):
    def _create_file_and_advance_dir_mtime(
        self,
        directory: str,
        filename: str,
        content: str = '',
        timeout_secs: float = 5.0,
    ) -> str:
        before = os.stat(directory).st_mtime_ns
        path = os.path.join(directory, filename)
        deadline = time.time() + timeout_secs
        while True:
            with open(path, 'w') as f:
                f.write(content)
            if os.stat(directory).st_mtime_ns != before:
                return path
            os.unlink(path)
            if time.time() >= deadline:
                self.fail(f"directory mtime for '{directory}' did not advance")
            time.sleep(0.02)

    def _assert_single_manifest_restart(self, output: str) -> None:
        self.assertEqual(
            output.count(
                'regeneration complete; restarting with updated manifest...'),
            1)
        self.assertEqual(output.count('Re-checking...'), 1)
        self.assertIn('ninja: no work to do.', output)

    def _escape_ninja_path(self, path: str) -> str:
        return path.replace('$', '$$').replace(':', '$:').replace(' ', '$ ')

    def _run_ninja_in_dir(
        self,
        cwd: str,
        args: T.Optional[T.List[str]] = None,
    ) -> str:
        cmd = [NINJA_PATH]
        if args:
            cmd.extend(args)
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=default_env,
            capture_output=True,
            check=False,
            text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, '')
        return proc.stdout

    def _short_path_alias(self, path: str) -> T.Optional[str]:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        get_short = kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short(path, buffer, len(buffer))
        if length == 0:
            return None
        return buffer.value

    def test_manifest_check_post_regen_ignores_removed_glob_watchfile(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            py = self._escape_ninja_path(sys.executable.replace('\\', '/'))
            watched = os.path.join(root, 'watched')
            os.mkdir(watched)
            with open(os.path.join(root, 'watch_dirs.txt'), 'w', newline='\n') as f:
                f.write('ninja_glob_watch_dirs_v1\n')
                f.write('watched\n')

            with open(os.path.join(root, 'touch.py'), 'w', newline='\n') as f:
                f.write('import pathlib,sys\n')
                f.write('pathlib.Path(sys.argv[1]).touch()\n')

            with open(os.path.join(root, 'regen.py'), 'w', newline='\n') as f:
                f.write(dedent(f"""\
import pathlib
import textwrap

root = pathlib.Path(__file__).resolve().parent
content = textwrap.dedent('''\
rule verify
  command = {py} regen.py
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = {py} touch.py $out
  description = touch $out

build build.ninja: verify
build out: touch
default out
''')
(root / 'build.ninja').write_text(content, newline='\\n')
watchfile = root / 'watch_dirs.txt'
if watchfile.exists():
    watchfile.unlink()
"""))

            with open(os.path.join(root, 'build.ninja'), 'w', newline='\n') as f:
                f.write(dedent(f'''\
rule verify
  command = {py} regen.py
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = {py} touch.py $out
  description = touch $out

build build.ninja: verify
  glob_watchfile = watch_dirs.txt
build out: touch
default out
'''))

            self.assertEqual(self._run_ninja_in_dir(root), '[1/1] touch out\n')
            self.assertEqual(self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(watched, 'new.txt')
            third = self._run_ninja_in_dir(root)
            self._assert_single_manifest_restart(third)
            self.assertIn('ninja: no work to do.', third)
            self.assertNotIn("glob watch file 'watch_dirs.txt' not found", third)
            self.assertEqual(
                self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

    def test_manifest_check_with_shortpath_absolute_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            short_root = self._short_path_alias(root)
            if not short_root:
                self.skipTest('8.3 short path alias is unavailable')
            if os.path.normcase(short_root) == os.path.normcase(root):
                self.skipTest('8.3 short path alias equals long path')

            src_dir = os.path.join(root, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass
            with open(os.path.join(root, 'touch.py'), 'w', newline='\n') as f:
                f.write('import pathlib,sys\n')
                f.write('pathlib.Path(sys.argv[1]).touch()\n')
            py = self._escape_ninja_path(sys.executable.replace('\\', '/'))

            with open(os.path.join(root, 'build.ninja'), 'w', newline='\n') as f:
                f.write(dedent(f'''\
rule verify
  command = {py} -c "pass"
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = {py} touch.py $out
  description = touch $out

build build.ninja: verify
build out: touch src/a.cpp
default out
'''))

            long_manifest = os.path.join(root, 'build.ninja')
            short_manifest = os.path.join(short_root, 'build.ninja')
            long_flags = ['-f', long_manifest]
            short_flags = ['-f', short_manifest]

            self.assertEqual(
                self._run_ninja_in_dir(root, long_flags), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(root, long_flags),
                'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
            third = self._run_ninja_in_dir(root, short_flags)
            self._assert_single_manifest_restart(third)

    def test_manifest_check_with_shortpath_relative_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            short_root = self._short_path_alias(root)
            if not short_root:
                self.skipTest('8.3 short path alias is unavailable')
            if os.path.normcase(short_root) == os.path.normcase(root):
                self.skipTest('8.3 short path alias equals long path')

            src_dir = os.path.join(root, 'src')
            os.mkdir(src_dir)
            with open(os.path.join(src_dir, 'a.cpp'), 'w'):
                pass
            with open(os.path.join(root, 'touch.py'), 'w', newline='\n') as f:
                f.write('import pathlib,sys\n')
                f.write('pathlib.Path(sys.argv[1]).touch()\n')
            py = self._escape_ninja_path(sys.executable.replace('\\', '/'))

            with open(os.path.join(root, 'build.ninja'), 'w', newline='\n') as f:
                f.write(dedent(f'''\
rule verify
  command = {py} -c "pass"
  description = Re-checking...
  pool = console
  restat = 1
  generator = 1

rule touch
  command = {py} touch.py $out
  description = touch $out

build build.ninja: verify
build out: touch src/a.cpp
default out
'''))

            self.assertEqual(self._run_ninja_in_dir(root), '[1/1] touch out\n')
            self.assertEqual(
                self._run_ninja_in_dir(root), 'ninja: no work to do.\n')

            self._create_file_and_advance_dir_mtime(src_dir, 'new.cpp')
            relative_short_manifest = os.path.relpath(
                os.path.join(short_root, 'build.ninja'), root)
            third = self._run_ninja_in_dir(
                root, ['-f', relative_short_manifest])
            self._assert_single_manifest_restart(third)


if __name__ == '__main__':
    unittest.main()
