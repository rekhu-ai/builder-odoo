"""Settings in ir.config_parameter, read and written the same way on every Odoo: 20 has typed methods (get_str,
set_str), 17 to 19 get_param and set_param."""


def get(env, key, default=False):
    params = env['ir.config_parameter'].sudo()
    if hasattr(params, 'get_str'):
        value = params.get_str(key, None)
        return default if value is None else value
    return params.get_param(key, default)


def put(env, key, value):
    params = env['ir.config_parameter'].sudo()
    if hasattr(params, 'set_str'):
        return params.set_str(key, value)
    return params.set_param(key, value)
