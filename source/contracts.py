from pydantic import (BaseModel, ConfigDict, Field, ValidationInfo,
    field_validator,
    model_validator,)
from typing import Literal

class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra = "forbid",
        str_strip_whitespace = True,
        validate_assignment = True
    )

class Design(StrictModel):
    chip_width: float = Field(gt=0)
    chip_height: float = Field(gt=0)
    grid_size: float = Field(gt=0)
    snap_terminals_to_grid: bool = True

    @model_validator(mode="after")
    def check_grid_fits(self) -> "Design":

        if self.grid_size > min(self.chip_width, self.chip_height):
            raise ValueError(
                f"Шаг сетки ({self.grid_size}) больше меньшей стороны чипа "
                f"({min(self.chip_width, self.chip_height)})"
            )
        if self.chip_width % self.grid_size != 0:
            raise ValueError(
                f"Ширина чипа ({self.chip_width}) не кратна шагу сетки "
                f"({self.grid_size})"
            )
        if self.chip_height % self.grid_size != 0:
            raise ValueError(
                f"Высота чипа ({self.chip_height}) не кратна шагу сетки "
                f"({self.grid_size})"
            )
        return self

class ClockSource(StrictModel):
    id: str = Field(min_length=1)
    x: float
    y: float
    pin_id: str = Field(min_length=1)
    input_capacitance: float = Field(default=0.0, ge=0)
    fixed: bool = True

class Sink(StrictModel):
    id: str = Field(min_length=1)
    x: float
    y: float
    pin_id: str = Field(min_length=1)
    capacitance: float = Field(gt=0)
    required_arrival_time: float
    fixed: bool = True

class Buffer(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["standard", "mid", "large"]
    input_capacitance: float = Field(gt=0)
    output_resistance: float = Field(gt=0)
    intrinsic_delay: float = Field(ge=0)
    drive_strength: float = Field(gt=0)
    available: bool = True

class WireModel(StrictModel):
    unit_capacitance: float = Field(ge=0)
    unit_resistance: float = Field(ge=0)
    unit_length: float = Field(default=1.0, gt=0)
    model: Literal["elmore", "linear"]

    @model_validator(mode="after")
    def check_not_all_zero(self) -> "WireModel":
        if self.unit_capacitance == 0:
            raise ValueError(
                "Емкость провода не может быть нулевой"
            )
        if self.unit_resistance == 0:
            raise ValueError(
                "Сопротивление провода не может быть нулевым"
            )
        return self

class TreeParams(StrictModel):
    algorithm: Literal["median_partition"] = "median_partition"
    max_fanout: int = Field(ge=2)
    min_sinks_for_partition: int = Field(default=2, ge=2)
    max_depth: int = Field(default=10, ge=1)
    partition_criterion: Literal[
        "balanced_capacitance", "balanced_count", "balanced_delay"
    ] = "balanced_capacitance"
    partition_axis: Literal["auto", "x", "y", "alternate"] = "auto"
    median_type: Literal[
        "geometric_median", "centroid", "manhattan_median"
    ] = "geometric_median"
    buffer_insertion: bool = True
    buffer_selection: Literal["size_by_load", "uniform", "min_delay"] = "size_by_load"
    min_buffer_input_cap: float = Field(default=0.5, ge=0)
    snaking_allowed: bool = True
    wire_snaking_limit: float = Field(default=0.2, ge=0, le=1.0)
    obstacle_aware: bool = False


class Optimization(StrictModel):
    objective: Literal["min_skew", "min_delay", "min_power"]
    secondary_objective: Literal["min_skew", "min_delay", "min_power"] | None = None
    max_iterations: int = Field(default=20, ge=1)
    convergence_tol: float = Field(default=1e-4, gt=0)
    skew_target: float | None = Field(default=None, ge=0)
    power_weight: float = Field(default=0.1, ge=0, le=1)
    delay_weight: float = Field(default=0.9, ge=0, le=1)

    @model_validator(mode="after")
    def check_consistency(self) -> "Optimization":
        # secondary не должен совпадать с основным
        if self.secondary_objective is not None and self.secondary_objective == self.objective:
            raise ValueError(
                f"Вторичная цель ({self.secondary_objective!r}) "
                f"совпадает с основной ({self.objective!r})"
            )
        # если objective не про skew, skew_target не имеет смысла
        if self.skew_target is not None and self.objective != "min_skew":
            # Это скорее предупреждение, чем ошибка — оставляем мягко
            pass
        return self

class InputFile(StrictModel):
    design: Design
    clock_source: ClockSource
    sinks: list[Sink] = Field(min_length=1)
    buffers: list[Buffer]
    wire_model: WireModel
    tree_params: TreeParams
    optimization: Optimization

    @field_validator("sinks")
    @classmethod
    def unique_sink_ids(cls, sinks: list[Sink]) -> list[Sink]:
        ids = [s.id for s in sinks]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Дублирующиеся id среди sinks: {dupes}")
        return sinks

    @field_validator("buffers")
    @classmethod
    def unique_buffer_ids(cls, buffers: list[Buffer]) -> list[Buffer]:
        ids = [b.id for b in buffers]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Дублирующиеся id среди buffers: {dupes}")
        return buffers


    @model_validator(mode="after")
    def check_consistency(self) -> "InputFile":

        self._check_inside_chip(
            self.clock_source.x, self.clock_source.y, f"clock_source {self.clock_source.id!r}"
        )

        g = self.design.grid_size
        if not self._on_grid(self.clock_source.x, g) or not self._on_grid(self.clock_source.y, g):
            raise ValueError(
                f"Координаты источника {self.clock_source.id!r} не кратны шагу сетки ({g})."
            )

        for s in self.sinks:
            self._check_inside_chip(s.x, s.y, f"sink {s.id!r}")
            if not self._on_grid(s.x, g):
                raise ValueError(f"X координата sinkа под id {s.id!r} не кратна шагу сетки!")
            if not self._on_grid(s.y, g):
                raise ValueError(f"Y координата sinkа под id {s.id!r} не кратна шагу сетки!")

        if self.tree_params.buffer_insertion and not self.buffers:
            raise ValueError(
                "Использование буферов разрешено, но список буферов пуст"
            )


        if self.tree_params.buffer_insertion:
            if not any(b.available for b in self.buffers):
                raise ValueError(
                    "Использование буферов разрешено, "
                    "но среди буферов нет ни одного доступного"
                )


        if self.tree_params.buffer_insertion:
            ok = [b for b in self.buffers if b.input_capacitance >= self.tree_params.min_buffer_input_cap]
            if not ok:
                raise ValueError(
                    f"min_buffer_input_cap={self.tree_params.min_buffer_input_cap} "
                    f"отсекает все буферы (у всех input_capacitance меньше)"
                )


        if self.sinks:
            import math
            needed_depth = math.ceil(
                math.log(len(self.sinks)) / math.log(self.tree_params.max_fanout)
            )
            if needed_depth > self.tree_params.max_depth:
                raise ValueError(
                    f"Максимальная глубина дерева={self.tree_params.max_depth} слишком мала для "
                    f"{len(self.sinks)} sinkов при максимальном числе потомков="
                    f"{self.tree_params.max_fanout} (нужно минимум {needed_depth})"
                )
        return self


    def _check_inside_chip(self, x: float, y: float, what: str) -> None:
        if not (0 <= x <= self.design.chip_width):
            raise ValueError(
                f"{what}: x={x} вне чипа [0, {self.design.chip_width}]"
            )
        if not (0 <= y <= self.design.chip_height):
            raise ValueError(
                f"{what}: y={y} вне чипа [0, {self.design.chip_height}]"
            )

    @staticmethod
    def _on_grid(value: float, grid_size: float) -> bool:
        import math
        return math.isclose(value % grid_size, 0.0, abs_tol=1e-9) or \
            math.isclose(value % grid_size, grid_size, abs_tol=1e-9)
