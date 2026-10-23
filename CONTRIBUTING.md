# Contributing

## What this edition is

This is a personal, unofficial edition of the [Swizzin dashboard](https://github.com/swizzin/swizzin_dashboard).
It is not endorsed by the Swizzin team, and there is no promise of support.
The [README](README.md) explains what is different from the official dashboard, and the licence question.

## Where to report what

- **A problem with Swizzin itself** (its install or remove scripts, `box`): report it to Swizzin, at
  https://github.com/swizzin/swizzin/issues
- **A problem with this edition** (the panel, the App Store, `heliedan.sh`): open an issue in this repository.
  Before posting, remove passwords, domain names, IP addresses and tokens.

## Security problems

Never report a security problem in a public issue.
Use the **Report a vulnerability** button in the **Security** tab of this repository.
The report stays private.

## Before a pull request

- One branch per change.
- Open the pull request against the `heliedan` branch.
- The tests must pass. They run automatically on every pull request.

## Running the tests on your computer

This is how the automatic tests run: Ubuntu 24.04 and its Python 3.12.
On Ubuntu, `python3 -m venv` needs the package `python3-venv`.

```bash
git clone -b heliedan https://github.com/Heliedan/swizzin_dashboard
cd swizzin_dashboard
python3 -m venv venv && venv/bin/pip install -r requirements.txt

# A copy of Swizzin at the commit named AUDITED in store-helper/removable.py,
# for tests/test_removable.py and tests/test_heliedan.py.
git clone https://github.com/swizzin/swizzin ../swizzin
git -C ../swizzin checkout "$(sed -n 's/^AUDITED = "\(.*\)"/\1/p' store-helper/removable.py)"

export SWIZZIN_SRC=../swizzin
for t in tests/test_*.py; do venv/bin/python "$t"; done
```

Each test file runs on its own, in its own Python process.
Every file must end with `OK`.
If `tests/test_removable.py` or `tests/test_heliedan.py` ends with `OK (skipped=...)`, it did not find Swizzin:
check `SWIZZIN_SRC`.
The automatic tests set `REQUIRE_SWIZZIN_SRC=1`: there, a wrong `SWIZZIN_SRC` makes them fail instead.
`tests/test_disk_alert_js.py` runs the dashboard's disk scripts in Node.js (the `node` command, no npm package).
Without `node`, its tests are skipped and it ends with `OK (skipped=...)`: install Node.js to run them.
The automatic tests set `REQUIRE_NODE=1`: there, a missing `node` makes it fail instead.
The tests do not need root: run them as a normal user.
As root, `tests/test_store_helper.py` ends with `OK (skipped=2)`: the helper always lets root in,
so the two tests of refused connections cannot run.

## Changes to the uninstall table

`store-helper/removable.py` says which apps the App Store may uninstall, and how.
Each entry was checked by hand against Swizzin's own remove scripts, at the commit `AUDITED`.
Any change to this file must be checked again against Swizzin's scripts.
Read the docstrings at the top of `store-helper/removable.py` and `tests/test_removable.py` first.
They explain the pinned sha256 of Swizzin's files.
`tests/test_removable.py` also lists every `rm` of every remove script, and why it is acceptable.
It fails when a script deletes something that this list does not explain.
