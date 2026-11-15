"""Application composition root for legacy and industry-neutral costing services."""
from pathlib import Path

from cost_core.registry import CostService, PluginRegistry, DatasetDefinition
from cost_core.costing import CostingPluginRegistry, CostingDatasetDefinition, CostingService
from industry_plugins.pharma.plugin import PharmaPlugin
from industry_plugins.pharma.policy import DEFAULT_DATASET_ID, PLUGIN_ID
from industry_plugins.costing_plugins import (
    PharmaCostingPlugin, RestaurantPlugin, ManufacturingPlugin,
)


def create_service(workspace_root=None):
    """Legacy pharma service retained for backwards compatibility."""
    root = Path(workspace_root) if workspace_root is not None else Path(__file__).resolve().parent.parent
    registry = PluginRegistry()
    registry.register(PharmaPlugin())
    return CostService(registry, (DatasetDefinition(
        dataset_id=DEFAULT_DATASET_ID, plugin_id=PLUGIN_ID,
        workspace_root=root.resolve(), label="药企模拟成本数据"),))


def create_costing_service(workspace_root=None, user_workspace=None):
    """Ledger-based service; user-imported datasets are scoped to one personal workspace."""
    root = Path(workspace_root) if workspace_root is not None else Path(__file__).resolve().parent.parent
    registry = CostingPluginRegistry()
    registry.register(RestaurantPlugin())
    registry.register(ManufacturingPlugin())
    registry.register(PharmaCostingPlugin(raw_root=root / "raw"))
    datasets = [
        CostingDatasetDefinition(dataset_id="restaurant-sample-v1",
            plugin_id="restaurant", label="餐饮模拟数据", version="1"),
        CostingDatasetDefinition(dataset_id="manufacturing-sample-v1",
            plugin_id="manufacturing", label="制造业模拟数据", version="1"),
        CostingDatasetDefinition(dataset_id=DEFAULT_DATASET_ID,
            plugin_id="pharma-costing", label="药企原始数据适配", version="1"),
    ]
    diagnostics = []
    if user_workspace is not None:
        import json
        import re
        import hashlib
        from industry_plugins.costing_plugins import GenericCsvPlugin, read_imported_sources
        user_root = Path(user_workspace).resolve()
        registry.register(GenericCsvPlugin(user_root))
        dataset_root = user_root / "costing_datasets"
        if dataset_root.exists():
            for manifest_path in sorted(dataset_root.glob("csv-*/manifest.json")):
                # In-progress imports are not datasets. Nor may a symlink map
                # one user's dataset name to someone else's directory.
                directory = manifest_path.parent
                if not re.fullmatch(r"csv-[0-9a-f]{16}", directory.name):
                    continue
                try:
                    if directory.is_symlink() or manifest_path.is_symlink():
                        raise ValueError("数据集目录不能是符号链接")
                    if not directory.resolve().is_relative_to(dataset_root.resolve()):
                        raise ValueError("数据集越出个人工作区")
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    dataset_id = manifest.get("dataset_id", "")
                    if directory.name != dataset_id:
                        raise ValueError("数据集目录与清单身份不一致")
                    schema = manifest.get("schema")
                    if not isinstance(schema, dict) or schema.get("plugin_id", "csv-generic") != "csv-generic":
                        raise ValueError("数据集字段映射或适配器无效")
                    if read_imported_sources(directory, manifest) is None:
                        csv_path = directory / "data.csv"
                        if csv_path.is_symlink() or not csv_path.is_file():
                            raise ValueError("原始 CSV 缺失或不是独立文件")
                        if manifest.get("data_sha256") != hashlib.sha256(csv_path.read_bytes()).hexdigest():
                            raise ValueError("原始 CSV 与导入时校验值不同")
                    datasets.append(CostingDatasetDefinition(dataset_id=dataset_id,
                        plugin_id="csv-generic", label=str(manifest.get("label", dataset_id)),
                        version=str(manifest.get("version", "1"))))
                except (OSError, ValueError, TypeError) as exc:
                    diagnostics.append({"dataset_id": directory.name, "status": "unavailable", "message": str(exc)})
    service = CostingService(registry, tuple(datasets))
    service.dataset_diagnostics = diagnostics
    return service
