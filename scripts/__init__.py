"""Funkuino — tooling to manage an ESPuino over the network.

This directory has two identities on purpose:

* In a checkout and in the macOS bundle it is a plain directory of modules that
  import each other flatly (``import espuino``), reached via PYTHONPATH.
* In a wheel it is installed as the package ``funkuino`` — that is only how the
  files get onto disk and how the ``funkuino`` console script finds
  :func:`funkuino.cli.main`. Nothing imports the modules *through* the package;
  ``cli.main`` puts this directory on ``sys.path`` and the flat imports work
  unchanged.

Keeping one import style is not cosmetic: ``espuino`` loaded both as ``espuino``
and as ``funkuino.espuino`` would be two module objects with two DATA_ROOTs and
two sets of manifest state, and the sync manifest is exactly the thing that must
not exist twice.
"""
