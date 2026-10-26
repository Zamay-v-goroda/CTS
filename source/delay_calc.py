from typing import Dict, Tuple
from models import ClockNode, WireModel

# Единицы: длина — мкм, ёмкость — фФ, сопротивление — Ом, задержка — нс.
# Ом * фФ = 1e-15 с = 1e-6 нс
OHM_FF_TO_NS = 1e-6


def get_manhattan_dist(n1: ClockNode, n2: ClockNode) -> float:
    """Вычисляет Манхэттенское расстояние между узлами """
    return abs(n1.x - n2.x) + abs(n1.y - n2.y)


class DelayCalculator:
    """
    Расчёт временных характеристик дерева синхронизации.
    Длина -> мкм, сопротивление -> Ом, ёмкость -> фФ, задержка -> нс.
    """

    def __init__(self, wire_model: WireModel):
        self.wire_model = wire_model
        self.model_type = wire_model.model.lower()
        self.scale = float(wire_model.unit_length)  # мкм на единицу координат

    # ------------------------------------------------------------------ public
    def calculate(self, root: ClockNode) -> Tuple[Dict[str, float], float]:
        """
        Возвращает ({id_sink: задержка в нс}, skew в нс).
        Длина провода = геометрия * unit_length + snake_length (змеевик).
        """
        self._update_capacitance_bottom_up(root)

        root.delay_from_source = 0.0
        sink_delays: Dict[str, float] = {}
        self._compute_delay_top_down(root, sink_delays)

        if not sink_delays:
            return {}, 0.0
        delays = list(sink_delays.values())
        return sink_delays, max(delays) - min(delays)

    def total_capacitance(self, root: ClockNode) -> float:
        """
        Суммарная переключаемая ёмкость дерева, фФ (провода + входы буферов + sink'и).
        Используется как оценка мощности: P ~ C * V^2 * f, где V и f постоянны.
        Вызывать после calculate() — длины проводов берутся оттуда.
        """
        total = 0.0
        stack = [root]
        while stack:
            node = stack.pop()
            if node.node_type == "sink" and node.sink_ref:
                total += node.sink_ref.capacitance
            elif node.node_type == "buffer" and node.buffer_ref:
                total += node.buffer_ref.input_capacitance
            if node.parent is not None:
                total += node.wire_length_to_parent * self.wire_model.unit_capacitance
            stack.extend(node.children)
        return total

    # ----------------------------------------------------------------- internal
    @staticmethod
    def _is_buffer(node: ClockNode) -> bool:
        return node.node_type == "buffer" and node.buffer_ref is not None

    def _link_children(self, node: ClockNode) -> float:
        """Обновляет длины проводов к детям и возвращает суммарную ёмкость (провода + дети), фФ"""
        total = 0.0
        for child in node.children:
            geometric = get_manhattan_dist(node, child) * self.scale
            child.wire_length_to_parent = geometric + child.snake_length
            wire_cap = child.wire_length_to_parent * self.wire_model.unit_capacitance
            total += wire_cap + self._update_capacitance_bottom_up(child)
        return total

    def _update_capacitance_bottom_up(self, node: ClockNode) -> float:
        """Проход снизу вверх: ёмкость, которую узел показывает своему родителю (фФ)."""
        if node.node_type == "sink" and node.sink_ref:
            node.downstream_capacitance = node.sink_ref.capacitance
            return node.downstream_capacitance

        if self._is_buffer(node):
            # Родитель видит только входную ёмкость буфера; нагрузка за буфером — отдельно
            node.buffer_output_load = self._link_children(node)
            node.downstream_capacitance = node.buffer_ref.input_capacitance
            return node.downstream_capacitance

        node.downstream_capacitance = self._link_children(node)
        return node.downstream_capacitance

    def _compute_delay_top_down(self, node: ClockNode, sink_delays: Dict[str, float]):
        """Проход сверху вниз: задержка каждого сегмента (нс)."""
        is_buf = self._is_buffer(node)
        if is_buf:
            intrinsic_delay = node.buffer_ref.intrinsic_delay            # нс
            r_driver = node.buffer_ref.output_resistance                  # Ом
            driver_load = node.buffer_output_load                         # фФ
        else:
            intrinsic_delay = 0.0
            r_driver = 0.0
            driver_load = node.downstream_capacitance

        for child in node.children:
            l_wire = child.wire_length_to_parent                          # мкм
            r_wire = l_wire * self.wire_model.unit_resistance             # Ом
            c_wire = l_wire * self.wire_model.unit_capacitance            # фФ
            c_load = child.downstream_capacitance                         # фФ

            if self.model_type == "elmore":
                rc = r_driver * driver_load + r_wire * (0.5 * c_wire + c_load)
                segment_delay = intrinsic_delay + rc * OHM_FF_TO_NS
            else:
                # Линейная модель: задержка пропорциональна длине провода
                segment_delay = (
                    intrinsic_delay
                    + r_driver * driver_load * OHM_FF_TO_NS
                    + l_wire * self.wire_model.linear_delay_per_um
                )

            child.delay_from_source = node.delay_from_source + segment_delay

            if child.node_type == "sink" and child.sink_ref:
                sink_delays[child.sink_ref.id] = child.delay_from_source

            self._compute_delay_top_down(child, sink_delays)
