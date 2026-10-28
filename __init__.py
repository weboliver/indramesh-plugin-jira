"""Package entry for the directory-scanned Jira plugin (Phase 292, JDATA-01).

The presence of this file is what makes the loader import ``plugins/jira`` as a
real Python package (``indramesh_plugin_jira``): ``module.py``'s relative import
(``from .database import Base``) resolves through that one canonical identity,
and the manifest's ``module`` field still names the entry submodule that defines
the plugin class.
"""