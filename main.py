import runpy

# Execute runner_live as if it were run as a script, but within package context
runpy.run_module('nova_scripts.Argus.runner_live', run_name='__main__')
