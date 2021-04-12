#!/usr/bin/env python3
# Copyright (c) 2020, 2021 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

# standard library imports only here
from pathlib import Path
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile

# 3rd party imports go here, if any are added.

# Portability:
#
# - Python 3.6 or later on POSIX
# - Python 3.7 or later on Windows (some os.PathLike features didn't
#   make it into 3.6 for Windows)

PROG = 'check-oss-history'

PARSER = argparse.ArgumentParser(
    prog=PROG,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description='''\
Checks that an sdk-nrf pull request which touches sdk-zephyr
has good structure.

WARNING: commit any local work and ensure a clean workspace before
         running this script.

"Good structure" currently means that the sdk-zephyr changes are
"rebasable", i.e. that:

1. The history can be rewritten into a linear series of commits onto
   the upstream merge-base using NCS tools like "west ncs-loot".
   (Note that this doesn't actually use "git rebase".)

2. The rewritten history has an empty diff with the sdk-zephyr
   revision in the sdk-nrf pull request.

This script changes local git repositories as follows:

- the workspace is updated to match nrf/west.yml
- sdk-zephyr: upstream master is fetched into refs/remotes/upstream/master,
  and an attempt is made to leave a rewritten history in the working tree

Commits with rewritten history are left as loose objects.
Feel free to delete these afterwards.
''')
PARSER.add_argument('--workspace', type=Path,
                    help='''where NCS workspace topdir should be;
                    the nrf repo should already be here''',
                    required=True)

ARGS = None                     # global arguments, see parse_args()

def stdout(*msg):
    # Print a diagnostic message to standard error.

    print(f'{PROG}:', *msg)
    sys.stdout.flush()

def parse_args():
    # Parse arguments into the ARGS global, validating them before
    # returning.

    global ARGS

    ARGS = PARSER.parse_args()
    if not ARGS.workspace or not ARGS.workspace.is_dir():
        PARSER.error(f'workspace "{ARGS.workspace}" is not a directory')

def ssplit(cmd):
    if isinstance(cmd, str):
        return shlex.split(cmd)

    return cmd

def runc(cmd, **kwargs):
    # A shorthand for running a simple shell command.
    cwd = kwargs.get('cwd', os.getcwd())
    stdout(f'running "{cmd}" in "{cwd}"')
    kwargs['check'] = True
    return subprocess.run(ssplit(cmd), **kwargs)

def runc_out(cmd, **kwargs):
    # A shorthand for running a simple shell command and getting its output.

    kwargs['check'] = True
    kwargs['stdout'] = subprocess.PIPE
    kwargs['universal_newlines'] = True
    cwd = kwargs.get('cwd', os.getcwd())
    stdout(f'running "{cmd}" in "{cwd}"')
    cp = subprocess.run(ssplit(cmd), **kwargs)
    return cp.stdout

def get_zephyr_merge_base():
    # Get the SHA of the upstream zephyr commit which is the
    # merge-base with the current sdk-zephyr HEAD.
    #
    # The zephyr repository must have an 'upstream' remote.

    stdout('------------------- finding zephyr merge base -------------------')

    zephyr = ARGS.workspace / 'zephyr'

    stdout('finding upstream zephyr main branch...')
    main_branch = get_head_branch('https://github.com/zephyrproject-rtos/zephyr')
    stdout(f'upstream zephyr main branch: {main_branch}')

    stdout('converting to SHA...')
    runc(f'git fetch -q upstream {main_branch}', cwd=zephyr)
    upstream_sha = runc_out('git rev-parse FETCH_HEAD', cwd=zephyr).strip()
    stdout(f'upstream/{main_branch} is at {upstream_sha}')

    stdout('finding merge-base...')
    merge_base = runc_out(f'git merge-base HEAD {upstream_sha}',
                          cwd=zephyr).strip()
    stdout(f"zephyr merge-base is {merge_base}")

    return merge_base

# simplified from west:
# https://github.com/zephyrproject-rtos/west/blob/3bdd02674ab0cce2babfd02494f0884b3f11fd4c/src/west/app/project.py#L315
def get_head_branch(url: str) -> str:
    # Get the branch which url's HEAD points to. Errors out if it
    # can't, prints a banner if it can.

    # The '--quiet' option disables printing the URL to stderr.
    output = runc_out(['git', 'ls-remote', '--quiet', '--symref', url, 'HEAD'])

    for line in output.splitlines():
        if not line.startswith('ref: '):
            continue
        # The output looks like this:
        #
        # ref: refs/heads/foo	HEAD
        # 6145ab537fcb3adc3ee77db5f5f95e661f1e91e6	HEAD
        #
        # So this is the 'ref: ...' case.
        #
        # Per git-check-ref-format(1), references can't have tabs
        # in them, so this doesn't have any weird edge cases.
        return line[len('ref: '):].split('\t')[0]

    # Unexpected output.
    raise RuntimeError(output)

def get_oot_zephyr_patches(zephyr_merge_base):
    # Print the OOT zephyr patches to the console and return a list of
    # their SHAs.

    stdout('------------------ getting OOT zephyr patches -------------------')

    fd, json_tmp = tempfile.mkstemp(prefix=f'{PROG}-', suffix='.json')
    os.close(fd)

    try:
        stdout("getting out of tree zephyr patches:")
        runc(f'west ncs-loot --zephyr-rev {zephyr_merge_base} '
             f'--json {json_tmp} zephyr', stdout=subprocess.DEVNULL,
             stderr=subprocess.DEVNULL, cwd=ARGS.workspace)
        with open(json_tmp, 'r') as f:
            json_output = json.load(f)
    finally:
        os.unlink(json_tmp)

    shas, shortlogs = (json_output['zephyr']['shas'],
                       json_output['zephyr']['shortlogs'])

    stdout("out of tree zephyr patches:")
    for sha, shortlog in zip(shas, shortlogs):
        stdout(f'- {sha} {shortlog}')

    return shas

def rewrite_zephyr_history(zephyr_merge_base, oot_zephyr_patches):
    # Create a rewritten zephyr history on top of zephyr_merge_base
    # which cherry-picks the given oot_zephyr_patches.

    stdout('--------------- trying to rewrite zephyr history ----------------')

    zephyr = ARGS.workspace / 'zephyr'

    before_sha = runc_out('west list -f {sha} zephyr', cwd=zephyr).strip()
    stdout(f'zephyr SHA in sdk-nrf PR manifest is {before_sha}')

    stdout(f'creating rewritten zephyr history on top of {zephyr_merge_base}')
    runc('git config user.name check-oss-history', cwd=zephyr)
    runc('git config user.email bot', cwd=zephyr)
    runc(f'git checkout {zephyr_merge_base}', cwd=zephyr)
    runc('git status', cwd=zephyr)
    for sha in oot_zephyr_patches:
        try:
            runc(f'git cherry-pick -x {sha}', cwd=zephyr)
        except subprocess.CalledProcessError as e:
            stdout(f'cherry-pick failed: {e}')

            stdout(f'checking if {sha} is a redundant commit...')
            runc('git cherry-pick --abort', cwd=zephyr)
            try:
                runc(f'git cherry-pick --keep-redundant-commits -x {sha}',
                     cwd=zephyr)
            except subprocess.CalledProcessError:
                stdout(f'{sha} is not a redundant commit; something is wrong '
                       'with either the sdk-zephyr changes or current downstream '
                       'history is malformed')
            else:
                stdout(f'{sha} is a redundant commit; do you need to revert '
                       'it before creating the [nrf mergeup] commit?')

            sys.exit(1)

    rebase_ref = runc_out('git rev-parse HEAD', cwd=zephyr).strip()
    stdout(f'leaving rewritten history HEAD ({rebase_ref}) '
           'in the working tree')

    return before_sha, rebase_ref

def check_zephyr_rewrite(before_sha, new_ref):
    # Checks struture of the rewritten zephyr history.

    stdout('------------------ checking rewritten history -------------------')

    zephyr = ARGS.workspace / 'zephyr'

    stdout(f'checking for empty diff in {zephyr} between {before_sha} '
           f'and {new_ref}')
    try:
        runc(f'git diff --exit-code {before_sha} {new_ref}', cwd=zephyr)
    except subprocess.CalledProcessError:
        sys.exit('diff is not empty; see above')

    stdout('''
      diff is empty! all good!

           ████
         ███ ██
         ██   █
         ██   ██
          ██   ███
           ██    ██
           ██     ███
            ██      ██
       ███████       ██
    █████              ███
   ██     ████          ██████
   ██  ████  ███             ██
   ██        ███             ██
    ██████████ ███           ██
    ██        ████           ██
    ███████████  ██          ██
      ██       ████     ██████
      ██████████ ██    ███
         ██     ████ ███
         █████████████

''')

def main():
    parse_args()
    zephyr_merge_base = get_zephyr_merge_base()
    oot_zephyr_patches = get_oot_zephyr_patches(zephyr_merge_base)
    before_sha, rebase_ref = rewrite_zephyr_history(zephyr_merge_base,
                                                    oot_zephyr_patches)
    check_zephyr_rewrite(before_sha, rebase_ref)

if __name__ == '__main__':
    main()
