from importlib import import_module

MODULE_TO_PACKAGE_HINTS = {
    "langchain_google_genai": "langchain-google-genai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_openai": "langchain-openai",
    "langchain_deepseek": "langchain-deepseek",
}


def _build_missing_dependency_hint(module_path: str, err: ImportError) -> str:
    """Build an actionable hint when module import fails."""
    module_root = module_path.split(".", 1)[0]
    missing_module = getattr(err, "name", None) or module_root

    # Prefer provider package hints for known integrations, even when the import
    # error is triggered by a transitive dependency (e.g. `google`).
    package_name = MODULE_TO_PACKAGE_HINTS.get(module_root)
    if package_name is None:
        package_name = MODULE_TO_PACKAGE_HINTS.get(missing_module, missing_module.replace("_", "-"))

    return f"Missing dependency '{missing_module}'. Install it with `uv add {package_name}` (or `pip install {package_name}`), then restart DeerFlow."


def resolve_variable[T](
    variable_path: str,
    expected_type: type[T] | tuple[type, ...] | None = None,
) -> T:
    """Resolve a variable from a path.

    Args:
        variable_path: The path to the variable (e.g. "parent_package_name.sub_package_name.module_name:variable_name").
        expected_type: Optional type or tuple of types to validate the resolved variable against.
            If provided, uses isinstance() to check if the variable is an instance of the expected type(s).

    Returns:
        The resolved variable.

    Raises:
        ImportError: If the module path is invalid or the attribute doesn't exist.
        ValueError: If the resolved variable doesn't pass the validation checks.

    工作方式（三步）：
      1. 在最后一个 ``:`` 处切分 ``variable_path`` → (module_path, variable_name)
      2. ``importlib.import_module(module_path)`` → 模块对象
      3. ``getattr(module, variable_name)`` → 加载后的工具/模型/类

    为什么用这个模式：整个工具、模型和沙箱提供者的加载链都依赖于
    ``resolve_variable``。在 config.yaml 中写入 ``use: deerflow.sandbox.
    tools:bash_tool`` 意味着 DeerFlow 无需在导入时就知道存在哪些工具——
    用户可以通过在配置中声明导入路径来添加第三方工具。
    ``expected_type`` 验证能尽早捕获配置错误（例如将 ``tools[].use``
    指向字符串而非 ``BaseTool`` 实例）。

    边界案例：
    - 路径中没有 ``:`` → rsplit(maxsplit=1) 抛出 ValueError，
      被重新提升为带有帮助信息的 ImportError。
    - 模块本身导入成功但传递依赖缺失 → 保留 Python 原始的 ImportError，
      如果根模块匹配已知的第三方包，则附加安装提示。
    """
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"{variable_path} doesn't look like a variable path. Example: parent_package_name.sub_package_name.module_name:variable_name") from err

    try:
        module = import_module(module_path)
    except ImportError as err:
        module_root = module_path.split(".", 1)[0]
        err_name = getattr(err, "name", None)
        if isinstance(err, ModuleNotFoundError) or err_name == module_root:
            hint = _build_missing_dependency_hint(module_path, err)
            raise ImportError(f"Could not import module {module_path}. {hint}") from err
        # Preserve the original ImportError message for non-missing-module failures.
        raise ImportError(f"Error importing module {module_path}: {err}") from err

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise ImportError(f"Module {module_path} does not define a {variable_name} attribute/class") from err

    # Type validation
    if expected_type is not None:
        if not isinstance(variable, expected_type):
            type_name = expected_type.__name__ if isinstance(expected_type, type) else " or ".join(t.__name__ for t in expected_type)
            raise ValueError(f"{variable_path} is not an instance of {type_name}, got {type(variable).__name__}")

    return variable


def resolve_class[T](class_path: str, base_class: type[T] | None = None) -> type[T]:
    """Resolve a class from a module path and class name.

    Args:
        class_path: The path to the class (e.g. "langchain_openai:ChatOpenAI").
        base_class: The base class to check if the resolved class is a subclass of.

    Returns:
        The resolved class.

    Raises:
        ImportError: If the module path is invalid or the attribute doesn't exist.
        ValueError: If the resolved object is not a class or not a subclass of base_class.
    """
    model_class = resolve_variable(class_path, expected_type=type)

    if not isinstance(model_class, type):
        raise ValueError(f"{class_path} is not a valid class")

    if base_class is not None and not issubclass(model_class, base_class):
        raise ValueError(f"{class_path} is not a subclass of {base_class.__name__}")

    return model_class
