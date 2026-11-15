"""Project policy: every generative request must use Flash."""
from errors import ConfigError

def require_flash(model):
    if model != "deepseek-flash":
        raise ConfigError("本工作区仅允许 deepseek-flash，已阻止其他型号")
    return model
