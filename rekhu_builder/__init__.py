from . import cli, controllers, models
from .lib import logs

# every process that loads the module keeps its warnings and errors where the builder can read them
logs.install()
