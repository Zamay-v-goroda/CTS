import json
import os
from typing import Dict, Any, Tuple, List, Optional
from models import ClockSource, Sink, BufferType, WireModel


def round_to_grid(coord: float, grid_size: float) -> float:
    """Округляет координату до ближайшего шага сетки grid_size"""
    if grid_size <= 0:
        return coord
    return round(coord / grid_size) * grid_size


# Допустимые значения перечисляемых параметров (по заданию)
ALLOWED_TREE = {
    "algorithm": ("median_partition",),
    "partition_criterion": ("balanced_capacitance", "balanced_count", "balanced_delay"),
    "partition_axis": ("auto", "x", "y", "alternate"),
    "median_type": ("geometric_median", "centroid", "manhattan_median", "bbox_center"),
    "buffer_selection": ("size_by_load", "uniform", "min_delay"),
}
# Старое имя из первой версии кода — оставлено как синоним
MEDIAN_ALIASES = {"geometric_center": "bbox_center"}
OBJECTIVES = ("min_skew", "min_delay", "min_power")

DESIGN_KEYS = ("chip_width", "chip_height", "grid_size", "snap_terminals_to_grid")

DESIGN_DEFAULTS: Dict[str, Any] = {
    # Округлять координаты терминалов до узлов сетки (по ТЗ узлы кратны grid_size).
    "snap_terminals_to_grid": True,
}

TREE_DEFAULTS: Dict[str, Any] = {
    "algorithm": "median_partition",
    "max_fanout": 4,
    "min_sinks_for_partition": 2,
    "max_depth": 10,
    "partition_criterion": "balanced_capacitance",
    "partition_axis": "auto",
    "median_type": "geometric_median",
    "buffer_insertion": True,
    "buffer_selection": "size_by_load",
    "min_buffer_input_cap": 0.0,
    "snaking_allowed": True,
    "wire_snaking_limit": 0.2,
    "obstacle_aware": False,
    # Не из задания: сколько фФ нагрузки «тянет» одна единица drive_strength
    # (используется только в buffer_selection = size_by_load)
    "load_per_drive_strength": 15.0,
}

OPT_DEFAULTS: Dict[str, Any] = {
    "objective": "min_skew",
    "secondary_objective": None,
    "max_iterations": 20,
    "convergence_tol": 1e-4,
    "skew_target": 0.05,
    "power_weight": 0.1,
    "delay_weight": 0.9,
}


class CTSParser:
    """Класс для загрузки и валидации входных данных CTS проекта"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.grid_size = 1.0
        self.chip_width = 0.0
        self.chip_height = 0.0
        self.snap_terminals = True

        self.clock_source: ClockSource = None
        self.sinks: List[Sink] = []
        self.buffers: Dict[str, BufferType] = {}  # только доступные (available: true)
        self.wire_model: WireModel = None
        self.tree_params: Dict[str, Any] = {}
        self.optimization_params: Dict[str, Any] = {}
        self.warnings: List[str] = []             # некритичные замечания по конфигурации

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _number(section: str, data: dict, key: str, default=None, minimum=None,
                strict_min: bool = False, integer: bool = False):
        if key not in data:
            if default is None:
                raise ValueError(f"{section}.{key}: обязательный параметр отсутствует.")
            value = default
        else:
            value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{section}.{key}: ожидалось число, получено {value!r}.")
        if integer:
            if int(value) != value:
                raise ValueError(f"{section}.{key}: ожидалось целое число, получено {value!r}.")
            value = int(value)
        else:
            value = float(value)
        if minimum is not None:
            if (value <= minimum) if strict_min else (value < minimum):
                sign = ">" if strict_min else ">="
                raise ValueError(f"{section}.{key}: значение {value} должно быть {sign} {minimum}.")
        return value

    @staticmethod
    def _flag(section: str, data: dict, key: str, default: bool) -> bool:
        value = data.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{section}.{key}: ожидалось true/false, получено {value!r}.")
        return value

    @staticmethod
    def _choice(section: str, data: dict, key: str, default: str, allowed: tuple,
                aliases: Optional[dict] = None) -> str:
        value = data.get(key, default)
        if not isinstance(value, str):
            raise ValueError(f"{section}.{key}: ожидалась строка, получено {value!r}.")
        value = value.strip().lower()
        if aliases:
            value = aliases.get(value, value)
        if value not in allowed:
            raise ValueError(
                f"{section}.{key}: недопустимое значение '{value}'. Допустимо: {', '.join(allowed)}."
            )
        return value

    # --------------------------------------------------------------------- parse
    def parse(self) -> Tuple[ClockSource, list, dict, WireModel, Dict[str, Any], Dict[str, Any]]:
        """
        Основной метод парсинга.
        Загружает JSON, проверяет и нормализует параметры (подставляет значения по умолчанию)
        и возвращает кортеж объектов.
        """
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Входной файл конфигурации не найден: {self.filepath}")

        with open(self.filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        for section in ("clock_source", "sinks", "wire_model"):
            if section not in data:
                raise ValueError(f"Во входном файле нет обязательной секции '{section}'.")

        self.warnings = []

        # 1. design
        design = data.get("design", {})
        for key in design:
            if key not in DESIGN_KEYS:
                self.warnings.append(f"design.{key}: неизвестный параметр, игнорируется.")
        self.grid_size = self._number("design", design, "grid_size", 1.0, minimum=0, strict_min=True)
        self.chip_width = self._number("design", design, "chip_width", 2000.0, minimum=0, strict_min=True)
        self.chip_height = self._number("design", design, "chip_height", 2000.0, minimum=0, strict_min=True)
        # Округлять ли координаты терминалов (sink/источник) до узлов сетки.
        # По ТЗ координаты узлов кратны grid_size; терминалы fixed, но при включённом
        # флаге они привязываются к сетке, и каждый сдвиг попадает в журнал.
        self.snap_terminals = self._flag("design", design, "snap_terminals_to_grid",
                                         DESIGN_DEFAULTS["snap_terminals_to_grid"])

        # 2. clock_source (фиксирован: координаты сдвигаются только для привязки к сетке)
        src = data["clock_source"]
        for key in ("id", "x", "y", "pin_id"):
            if key not in src:
                raise ValueError(f"clock_source.{key}: обязательный параметр отсутствует.")
        if src.get("fixed", True) is not True:
            self.warnings.append(
                "clock_source.fixed=false заменено на true: по ТЗ источник такта не перемещается."
            )
        src_x = self._number("clock_source", src, "x")
        src_y = self._number("clock_source", src, "y")
        if self.snap_terminals:
            src_x = self._snap_terminal("clock_source", str(src["id"]), "x", src_x)
            src_y = self._snap_terminal("clock_source", str(src["id"]), "y", src_y)
        self.clock_source = ClockSource(
            id=str(src["id"]),
            x=src_x,
            y=src_y,
            pin_id=str(src["pin_id"]),
            input_capacitance=self._number("clock_source", src, "input_capacitance", 0.0, minimum=0),
            fixed=True,  # по заданию источник всегда fixed
        )
        self.validate_bounds(self.clock_source.x, self.clock_source.y, self.clock_source.id)

        # 3. sinks (фиксированы: координаты сдвигаются только для привязки к сетке)
        seen_ids = {self.clock_source.id}
        self.sinks = []
        for i, sd in enumerate(data["sinks"]):
            for key in ("id", "x", "y", "pin_id", "capacitance", "required_arrival_time"):
                if key not in sd:
                    raise ValueError(f"sinks[{i}].{key}: обязательный параметр отсутствует.")
            sid = str(sd["id"])
            if sid in seen_ids:
                raise ValueError(f"Повторяющийся идентификатор '{sid}' (sinks[{i}]).")
            seen_ids.add(sid)
            if sd.get("fixed", True) is not True:
                self.warnings.append(
                    f"sink {sid!r}: fixed=false заменено на true: по ТЗ sink'и не перемещаются."
                )
            sk_x = self._number(f"sinks[{i}]", sd, "x")
            sk_y = self._number(f"sinks[{i}]", sd, "y")
            if self.snap_terminals:
                sk_x = self._snap_terminal(f"sink", sid, "x", sk_x)
                sk_y = self._snap_terminal(f"sink", sid, "y", sk_y)
            sink = Sink(
                id=sid,
                x=sk_x,
                y=sk_y,
                pin_id=str(sd["pin_id"]),
                capacitance=self._number(f"sinks[{i}]", sd, "capacitance", minimum=0),
                required_arrival_time=self._number(f"sinks[{i}]", sd, "required_arrival_time", minimum=0),
                fixed=True,  # по заданию sink'и всегда fixed
            )
            self.validate_bounds(sink.x, sink.y, sink.id)
            self.sinks.append(sink)
        if not self.sinks:
            raise ValueError("Список sinks пуст: дерево синхронизации строить не для чего.")

        # 4. buffers (в словарь попадают только доступные)
        self.buffers = {}
        for i, bd in enumerate(data.get("buffers", [])):
            for key in ("id", "type", "input_capacitance", "output_resistance",
                        "intrinsic_delay", "drive_strength"):
                if key not in bd:
                    raise ValueError(f"buffers[{i}].{key}: обязательный параметр отсутствует.")
            buf = BufferType(
                id=str(bd["id"]),
                type=str(bd["type"]),
                input_capacitance=self._number(f"buffers[{i}]", bd, "input_capacitance", minimum=0),
                output_resistance=self._number(f"buffers[{i}]", bd, "output_resistance", minimum=0),
                intrinsic_delay=self._number(f"buffers[{i}]", bd, "intrinsic_delay", minimum=0),
                drive_strength=self._number(f"buffers[{i}]", bd, "drive_strength", minimum=0, strict_min=True),
                available=self._flag(f"buffers[{i}]", bd, "available", True),
            )
            if buf.available:
                self.buffers[buf.id] = buf

        # 5. wire_model
        wm = data["wire_model"]
        self.wire_model = WireModel(
            unit_capacitance=self._number("wire_model", wm, "unit_capacitance", minimum=0),
            unit_resistance=self._number("wire_model", wm, "unit_resistance", minimum=0),
            unit_length=self._number("wire_model", wm, "unit_length", minimum=0, strict_min=True),
            model=self._choice("wire_model", wm, "model", "elmore", ("elmore", "linear")),
            linear_delay_per_um=self._number("wire_model", wm, "linear_delay_per_um", 0.001, minimum=0),
        )

        # 6. tree_params и optimization
        self.tree_params = self._normalize_tree_params(data.get("tree_params", {}))
        self.optimization_params = self._normalize_opt_params(data.get("optimization", {}))

        # 7. Перекрёстные проверки
        tp = self.tree_params
        if tp["buffer_insertion"]:
            eligible = [b for b in self.buffers.values()
                        if b.input_capacitance >= tp["min_buffer_input_cap"]]
            if not eligible:
                raise ValueError(
                    "buffer_insertion = true, но нет доступных буферов с input_capacitance >= "
                    f"min_buffer_input_cap ({tp['min_buffer_input_cap']})."
                )
        capacity = tp["max_fanout"] ** (tp["max_depth"] - 1)
        if len(self.sinks) > capacity:
            raise ValueError(
                f"Дерево глубины {tp['max_depth']} с fanout {tp['max_fanout']} вмещает не более "
                f"{capacity} sink'ов, во входном файле их {len(self.sinks)}. "
                "Увеличьте max_depth или max_fanout."
            )

        return (self.clock_source, self.sinks, self.buffers, self.wire_model,
                self.tree_params, self.optimization_params)

    # ------------------------------------------------------------- normalization
    def _normalize_tree_params(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        sec = "tree_params"
        for key in raw:
            if key not in TREE_DEFAULTS:
                self.warnings.append(f"tree_params.{key}: неизвестный параметр, игнорируется.")

        tp: Dict[str, Any] = {}
        tp["algorithm"] = self._choice(sec, raw, "algorithm", TREE_DEFAULTS["algorithm"],
                                       ALLOWED_TREE["algorithm"])
        tp["max_fanout"] = self._number(sec, raw, "max_fanout", TREE_DEFAULTS["max_fanout"],
                                        minimum=2, integer=True)
        tp["min_sinks_for_partition"] = self._number(sec, raw, "min_sinks_for_partition",
                                                     TREE_DEFAULTS["min_sinks_for_partition"],
                                                     minimum=2, integer=True)
        tp["max_depth"] = self._number(sec, raw, "max_depth", TREE_DEFAULTS["max_depth"],
                                       minimum=2, integer=True)
        tp["partition_criterion"] = self._choice(sec, raw, "partition_criterion",
                                                 TREE_DEFAULTS["partition_criterion"],
                                                 ALLOWED_TREE["partition_criterion"])
        tp["partition_axis"] = self._choice(sec, raw, "partition_axis", TREE_DEFAULTS["partition_axis"],
                                            ALLOWED_TREE["partition_axis"])
        tp["median_type"] = self._choice(sec, raw, "median_type", TREE_DEFAULTS["median_type"],
                                         ALLOWED_TREE["median_type"], MEDIAN_ALIASES)
        tp["buffer_insertion"] = self._flag(sec, raw, "buffer_insertion", TREE_DEFAULTS["buffer_insertion"])
        tp["buffer_selection"] = self._choice(sec, raw, "buffer_selection", TREE_DEFAULTS["buffer_selection"],
                                              ALLOWED_TREE["buffer_selection"])
        tp["min_buffer_input_cap"] = self._number(sec, raw, "min_buffer_input_cap",
                                                  TREE_DEFAULTS["min_buffer_input_cap"], minimum=0)
        tp["snaking_allowed"] = self._flag(sec, raw, "snaking_allowed", TREE_DEFAULTS["snaking_allowed"])
        tp["wire_snaking_limit"] = self._number(sec, raw, "wire_snaking_limit",
                                                TREE_DEFAULTS["wire_snaking_limit"], minimum=0)
        tp["obstacle_aware"] = self._flag(sec, raw, "obstacle_aware", TREE_DEFAULTS["obstacle_aware"])
        tp["load_per_drive_strength"] = self._number(sec, raw, "load_per_drive_strength",
                                                     TREE_DEFAULTS["load_per_drive_strength"],
                                                     minimum=0, strict_min=True)

        if tp["obstacle_aware"]:
            self.warnings.append(
                "tree_params.obstacle_aware = true, но во входном формате нет описания препятствий: "
                "параметр принят, на построение дерева не влияет."
            )
        return tp

    def _normalize_opt_params(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        sec = "optimization"
        for key in raw:
            if key not in OPT_DEFAULTS:
                self.warnings.append(f"optimization.{key}: неизвестный параметр, игнорируется.")

        op: Dict[str, Any] = {}
        op["objective"] = self._choice(sec, raw, "objective", OPT_DEFAULTS["objective"], OBJECTIVES)

        secondary = raw.get("secondary_objective", None)
        if secondary in (None, "", "none", "None"):
            op["secondary_objective"] = None
        else:
            op["secondary_objective"] = self._choice(sec, raw, "secondary_objective", "", OBJECTIVES)
            if op["secondary_objective"] == op["objective"]:
                self.warnings.append(
                    "optimization.secondary_objective совпадает с objective, вторичная цель игнорируется."
                )
                op["secondary_objective"] = None

        op["max_iterations"] = self._number(sec, raw, "max_iterations", OPT_DEFAULTS["max_iterations"],
                                            minimum=0, integer=True)
        op["convergence_tol"] = self._number(sec, raw, "convergence_tol",
                                             OPT_DEFAULTS["convergence_tol"], minimum=0)
        op["skew_target"] = self._number(sec, raw, "skew_target", OPT_DEFAULTS["skew_target"], minimum=0)
        op["power_weight"] = self._number(sec, raw, "power_weight", OPT_DEFAULTS["power_weight"], minimum=0)
        op["delay_weight"] = self._number(sec, raw, "delay_weight", OPT_DEFAULTS["delay_weight"], minimum=0)
        if op["power_weight"] == 0 and op["delay_weight"] == 0:
            raise ValueError("optimization: power_weight и delay_weight не могут быть одновременно равны 0.")
        return op

    def validate_bounds(self, x: float, y: float, entity_id: str):
        """Проверяет, что объект не вышел за границы кристалла"""
        if not (0 <= x <= self.chip_width) or not (0 <= y <= self.chip_height):
            raise ValueError(
                f"Элемент '{entity_id}' находится вне границ кристалла! "
                f"Координаты: ({x}, {y}), Размер кристалла: {self.chip_width}x{self.chip_height}"
            )

    def _snap_terminal(self, what: str, entity_id: str, axis: str, value: float) -> float:
        """Округляет координату терминала к ближайшему узлу сетки и логирует сдвиг."""
        g = self.grid_size
        if g <= 0:
            return value
        limit = self.chip_width if axis == "x" else self.chip_height
        snapped = min(max(round(value / g) * g, 0.0), limit)
        if abs(snapped - value) > 1e-9:
            self.warnings.append(
                f"{what} {entity_id!r}: координата {axis}={value} не кратна сетке ({g}), "
                f"округлена до {snapped}."
            )
        return float(snapped)
