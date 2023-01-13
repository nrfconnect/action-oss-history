#!/usr/bin/env python3
# Copyright (c) 2020, 2021 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

# standard library imports only here
from typing import Dict, List
from pathlib import Path
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

# 3rd party imports go here, if any are added.

# Portability:
#
# - Python 3.6 or later on POSIX
# - Python 3.7 or later on Windows (some os.PathLike features didn't
#   make it into 3.6 for Windows)

# Extend this list of nrf/west.yml project names as necessary.
# Every project in this list will have its downstream history checked
# by default when run as a GitHub action. You can override this
# at the command line.
DEFAULT_PROJECTS_TO_CHECK = [
    'zephyr',
    'mbedtls',
    'mcuboot',
    'trusted-firmware-m'
]

PROG = 'oss-history'

ZEPHYR_URL = 'https://github.com/zephyrproject-rtos/zephyr'

PARSER = argparse.ArgumentParser(
    prog=PROG,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description=f'''\
Checks that an sdk-nrf west.yml has "rebasable" history
in its open source software (OSS) repositories.

Rebasing OSS trees
------------------

To run this manually to rebase the workspace during a release,
do something like this:

  python3 action.py -w ~/ncs --no-user-config --quiet-subprocesses

Notes:

- Repositories we do not fork from zephyr/west.yml cannot be rebased
  in this way. Do these by hand.

- If sdk-zephyr history is based on a branch other than 'main', add
  '--zephyr-merge-base X', where 'X' is the latest upstream zephyr commit
  in sdk-zephyr.

"Rebasable" history
-------------------

This means:

1. The history can be rewritten into a linear series of commits onto
   the upstream merge-base from zephyr/west.yml using the output of
   NCS extension command "west ncs-loot".

   Note that this doesn't actually use "git rebase".

2. The rewritten history has an empty diff with whatever
   revision is in the sdk-nrf pull request.

What this script does
---------------------

By default, this script fetches the latest upstream commit 'C'
from {ZEPHYR_URL}.
(This is left in FETCH_HEAD in the zephyr repository.)
The 'git merge-base' of the current sdk-zephyr revision and 'C' is used
as a "target" commit to rebase zephyr history on top of. The project
revisions in zephyr/west.yml at this target commit are used to rebase
the history of other projects.

If you want to skip this fetch or use another merge base commit, use
--zephyr-merge-base to set it manually.

To check history, this script clones local git repositories as needed
into an 'oss-history' subdirectory of the workspace. The history is
rewritten in the clone, so your working trees are not affected. The
user.name and user.email git configuration variables in the clones are
set to "oss-history" and "bot" respectively unless --no-user-config is
given, to avoid issues in action environments where these variables
are typically not set.

The rewritten history is left as a detached HEAD in the clone under
'oss-history'. Feel free to delete this directory afterwards.
''')
PARSER.add_argument('-w', '--workspace', type=Path, required=True,
                    help='NCS workspace topdir')
PARSER.add_argument('-p', '--project', dest='projects', action='append',
                    help='project to check; may be given multiple times')
PARSER.add_argument('-f', '--force', action='store_true',
                    help=f'''delete any repositories under <workspace>/{PROG}
                    that already exist''')
PARSER.add_argument('-z', '--zephyr-merge-base', metavar='REF',
                    help='''zephyr git ref (commit, branch, etc.)
                    to use as a merge-base; default fetches from upstream''')
PARSER.add_argument('--no-user-config', action='store_true',
                    help="""don't reconfigure user-specific git
                    options in repository clones""")
PARSER.add_argument('--quiet-subprocesses', action='store_true',
                    help='silence output related to running subprocesses')

ARGS = None                     # global arguments, see parse_args()

# Type for git SHAs, for readability. Just a string.
Sha = str

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

    cwd = os.fspath(kwargs.get('cwd', os.getcwd()))

    if ARGS.quiet_subprocesses:
        kwargs['stdout'] = subprocess.DEVNULL
        kwargs['stderr'] = subprocess.DEVNULL
    else:
        stdout(f'running "{cmd}" in "{cwd}"')

    kwargs['check'] = True
    return subprocess.run(ssplit(cmd), **kwargs)

def runc_out(cmd, **kwargs):
    # A shorthand for running a simple shell command and getting its output.

    cwd = kwargs.get('cwd', os.getcwd())

    if ARGS.quiet_subprocesses:
        kwargs['stderr'] = subprocess.DEVNULL
    else:
        stdout(f'running "{cmd}" in "{cwd}"')

    kwargs['check'] = True
    kwargs['universal_newlines'] = True
    kwargs['stdout'] = subprocess.PIPE
    cp = subprocess.run(ssplit(cmd), **kwargs)
    return cp.stdout

def get_merge_base(path, upstream_url, branch=None):
    # Get the SHA of the tip commit in 'branch' from 'upstream_url'
    # which is the merge-base with the current HEAD in the repository
    # at 'path'.

    stdout('-' * 79)
    stdout(f'{path}: getting upstream merge base from {upstream_url}')

    if branch is None:
        stdout(f'getting upstream main branch...')
        branch = get_head_branch(upstream_url)
        stdout(f'upstream main branch: {branch}')

    stdout(f'converting branch "{branch}" to SHA...')
    runc(f'git fetch {upstream_url} {branch}', cwd=path)
    upstream_sha = runc_out('git rev-parse FETCH_HEAD', cwd=path).strip()
    stdout(f'branch "{branch}" is at commit {upstream_sha}')

    stdout('finding merge-base...')
    merge_base = runc_out(f'git merge-base HEAD {upstream_sha}',
                          cwd=path).strip()
    stdout(f'the merge-base is {merge_base}')

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

def get_ncs_loot(zephyr_rev: Sha, projects: List[str]) -> Dict[str, Dict]:
    # - zephyr_rev: zephyr revision to pass to west ncs-loot
    # - projects: list of project names whose loot to get
    #
    # Returns the 'west ncs-loot' output for those projects as a
    # parsed JSON object. The keys in the return value are the project
    # names.

    stdout('-' * 79)
    stdout('getting out of tree commit info using west ncs-loot')

    fd, json_tmp = tempfile.mkstemp(prefix=f'{PROG}-', suffix='.json')
    os.close(fd)

    try:
        runc('west ncs-loot '
             f'--zephyr-rev {zephyr_rev} '
             f'--json {json_tmp} ' +
             ' '.join(projects),
             cwd=ARGS.workspace)
        with open(json_tmp, 'r') as f:
            json_output = json.load(f)
    finally:
        os.unlink(json_tmp)

    return json_output

def synchronize_into(project_name, from_path, to_path):
    # Clone 'from_path' into 'to_path'. If 'to_path' exists,
    # it is deleted it first if ARGS.force is given, but otherwise,
    # an error is raised.

    stdout(f'cloning {project_name} into {to_path}')

    if to_path.exists():
        if not ARGS.force:
            sys.exit(f'error: path exists: {to_path}.\n'
                     f'Remove {to_path.parent}, or use --force.')
        else:
            shutil.rmtree(to_path.parent)

    runc(f'git clone {from_path} {to_path}')

def rewrite_history(path: Path, base_commit: Sha, patches: List[Sha]):
    # Create a rewritten history in the git repository at 'path',
    # cherry-picking 'patches' on top of 'base_commit'.

    stdout(f'rewriting history in {path} onto {base_commit}')
    runc(f'git checkout {base_commit}', cwd=path)
    runc('git status', cwd=path)

    # The base command to use when attempting to cherry-pick a SHA.
    #
    # We use the 'ours' option because it seems to help resolving
    # problems when the same change has been applied in separate
    # upstream and downstream commits. (In this case, 'ours' seems to
    # be the tree we are rewriting history onto, not the tree we are
    # rewriting history from.)
    #
    # Without the 'ours' option, we can get conflicts in scenarios
    # similar to this hypothetical commit history:
    #
    #     M
    #     |\
    #     X \
    #     |  Y
    #     .  |
    #     .  Z
    #        |
    #        .
    #        .
    #
    # Above, out of tree commit X contains some of the same changes as
    # upstream Z. Upstream commit Y provides further changes to the
    # hunk which is touched by both X and Z.
    #
    # If X is not a redundant commit because it contains other changes
    # not reflected in Z or anywhere else upstream, we have observed
    # cases where the upmerge commit M can be resolved without
    # conflicts, but we subsequently run into errors when
    # cherry-picking X onto the new upstream history, due to conflicts
    # with Y. Choosing the 'ours' merge strategy option seems to help here,
    # and it can't result in an erroneous result from this script
    # because we still check that the final rewritten history has no
    # diff with the original before exiting.

    CHERRY_PICK = \
        'git cherry-pick --strategy ort --strategy-option ours -x'

    for sha in patches:
        try:
            runc(f'{CHERRY_PICK} {sha}', cwd=path)
        except subprocess.CalledProcessError as e:
            stdout(f'cherry-pick failed: {e}')

            stdout(f'checking if {sha} is a redundant commit...')
            runc('git cherry-pick --abort', cwd=path)
            try:
                runc(f'{CHERRY_PICK} --keep-redundant-commits {sha}',
                     cwd=path)
            except subprocess.CalledProcessError:
                stdout(f'{sha} is not a redundant commit; something looks wrong '
                       'with either the patches to apply or current history')
            else:
                stdout(f'{sha} is a redundant commit; do you need to revert '
                       'it before creating the [nrf mergeup] commit?')

            sys.exit(1)

    rewrite_sha = runc_out('git rev-parse HEAD', cwd=path).strip()
    stdout(f'leaving rewritten history in the working tree at {rewrite_sha}')

    return rewrite_sha

def check_history_rewrite(path: Path, before_sha: Sha, rewrite_sha: str):
    # Checks struture of the 'rewritten' history.

    stdout(f'checking for empty diff between old and new history...')
    try:
        runc(f'git diff --exit-code {before_sha} {rewrite_sha}', cwd=path)
    except subprocess.CalledProcessError:
        sys.exit('diff is not empty; see above')

    stdout('OK! diff is empty')

def all_good():
    stdout('''

    All checked projects have clean history.

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

    zephyr = ARGS.workspace / 'zephyr'
    if not zephyr.is_dir():
        sys.exit(f'zephyr {zephyr} does not exist; check workspace '
                 f'{ARGS.workspace} ({ARGS.workspace.resolve()}), '
                 'which contains: ' + list(ARGS.workspace.iterdir()))

    if ARGS.zephyr_merge_base is not None:
        zephyr_merge_base = ARGS.zephyr_merge_base
    else:
        zephyr_merge_base = get_merge_base(zephyr,
                                           ZEPHYR_URL,
                                           branch='main')
    ncs_loot = get_ncs_loot(zephyr_merge_base,
                            ARGS.projects or DEFAULT_PROJECTS_TO_CHECK)

    for project_name, loot in ncs_loot.items():
        stdout('-' * 79)
        stdout(f'checking: {project_name}')

        for sha, shortlog in zip(loot['shas'], loot['shortlogs']):
            if shortlog.rstrip().endswith('...'):
                sys.exit(f'''\
{project_name}: commit {sha} shortlog ends with "...": {shortlog}

It is no longer necessary to shorten upstream shortlogs to fit inside
line length limits. Please use the full upstream shortlog instead.
''')

        from_path = (ARGS.workspace / loot['path']).resolve()
        to_path = (ARGS.workspace / 'oss-history' / project_name).resolve()
        synchronize_into(project_name, from_path, to_path)
        if not ARGS.no_user_config:
            stdout(f'overriding user configs in {to_path}')
            runc('git config user.name oss-history', cwd=to_path)
            runc('git config user.email bot', cwd=to_path)
        rewrite_sha = rewrite_history(to_path, loot['upstream-commit'], loot['shas'])
        check_history_rewrite(to_path, loot['ncs-commit'], rewrite_sha)

    all_good()

if __name__ == '__main__':
    main()
