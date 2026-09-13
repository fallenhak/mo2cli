# Contributing

Contributions and focused bug reports are welcome. Open an issue before a large behavioral change so its MO2 compatibility and recovery model can be agreed first.

## Development setup

```powershell
git clone https://github.com/fallenhak/mo2cli.git
cd mo2cli
py -3.13 -m pip install -e ".[dev]"
```

## Before submitting a change

Add a regression test for every bug fix and run:

```powershell
py -3.13 -m unittest discover -s tests -v
py -3.13 -m compileall mo2cli
ruff check --select E9,F63,F7,F82 mo2cli tests
py -3.13 -m build
twine check dist/*
```

Keep filesystem mutations transactional and scoped to the selected MO2 instance. Preserve existing user data on failure, validate archive paths before extraction, and avoid embedding personal paths, credentials, or third-party mod files in tests.
