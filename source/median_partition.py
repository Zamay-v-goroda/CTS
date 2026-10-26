import math
import statistics
from bisect import bisect_left
from typing import List, Dict, Any, Tuple, Optional
from models import ClockNode, ClockSource, Sink, BufferType, WireModel
from delay_calc import OHM_FF_TO_NS


class MedianPartitionCTS:
    """
    Синтез дерева синхронизации рекурсивным медианным разбиением.

    Обрабатываемые параметры tree_params:
      algorithm, max_fanout, min_sinks_for_partition, max_depth,
      partition_criterion (balanced_capacitance | balanced_count | balanced_delay),
      partition_axis (auto | x | y | alternate),
      median_type (geometric_median | centroid | manhattan_median | bbox_center),
      buffer_insertion, buffer_selection (size_by_load | uniform | min_delay),
      min_buffer_input_cap.
    (snaking_allowed / wire_snaking_limit / obstacle_aware обрабатываются в optimizer / parser.)
    """

    def __init__(
        self,
        clock_source: ClockSource,
        sinks: List[Sink],
        buffers: Dict[str, BufferType],
        wire_model: WireModel,
        tree_params: Dict[str, Any],
        grid_size: float = 1.0,
        chip_width: Optional[float] = None,
        chip_height: Optional[float] = None,
    ):
        self.clock_source = clock_source
        self.sinks = sinks
        self.buffers = buffers
        self.wire_model = wire_model
        self.params = tree_params

        p = tree_params
        if p.get("algorithm", "median_partition") != "median_partition":
            raise ValueError(f"Неподдерживаемый алгоритм: {p.get('algorithm')}")

        self.max_fanout = int(p.get("max_fanout", 4))
        self.min_sinks = int(p.get("min_sinks_for_partition", 2))
        self.max_depth = int(p.get("max_depth", 10))
        self.criterion = p.get("partition_criterion", "balanced_capacitance")
        self.axis_mode = p.get("partition_axis", "auto")
        self.median_type = p.get("median_type", "geometric_median")
        self.buffer_insertion = bool(p.get("buffer_insertion", True))
        self.buffer_selection = p.get("buffer_selection", "size_by_load")
        self.min_buffer_cap = float(p.get("min_buffer_input_cap", 0.0))
        self.load_per_drive = float(p.get("load_per_drive_strength", 15.0))

        if self.max_fanout < 2:
            raise ValueError("max_fanout должен быть не меньше 2.")

        # Шаг сетки и габариты берутся из секции design (передаются из main)
        self.grid_size = float(grid_size)
        self.chip_width = chip_width
        self.chip_height = chip_height
        self.scale = float(wire_model.unit_length)

        # Буферы, допустимые по min_buffer_input_cap, от слабого к сильному
        self.eligible_buffers: List[BufferType] = sorted(
            (b for b in buffers.values() if b.available and b.input_capacitance >= self.min_buffer_cap),
            key=lambda b: (b.drive_strength, b.id),
        )
        if self.buffer_insertion and not self.eligible_buffers:
            raise ValueError("Нет доступных буферов, удовлетворяющих min_buffer_input_cap.")

        self.node_counter = 0

    # ------------------------------------------------------------------ build
    def _next_node_id(self, prefix: str) -> str:
        self.node_counter += 1
        return f"{prefix}_{self.node_counter}"

    def _capacity(self, depth: int) -> int:
        """Сколько sink'ов максимум помещается под узлом на глубине depth (корень = 1)."""
        return self.max_fanout ** max(self.max_depth - depth, 0)

    def build_tree(self) -> ClockNode:
        root = ClockNode(
            id=self.clock_source.id,
            x=self.clock_source.x,
            y=self.clock_source.y,
            node_type="source",
        )
        if not self.sinks:
            return root

        if len(self.sinks) > self._capacity(1):
            raise ValueError(
                f"{len(self.sinks)} sink'ов не помещаются в дерево: max_depth={self.max_depth}, "
                f"max_fanout={self.max_fanout} (максимум {self._capacity(1)})."
            )

        self._partition_recursive(root, list(self.sinks), depth=1)
        if self.buffer_insertion:
            self._assign_buffers(root)
        return root

    def _attach_sink(self, parent: ClockNode, sk: Sink):
        parent.add_child(ClockNode(id=sk.id, x=sk.x, y=sk.y, node_type="sink", sink_ref=sk))

    def _partition_recursive(self, parent_node: ClockNode, current_sinks: List[Sink], depth: int):
        """depth — глубина parent_node (корень = 1)."""
        n = len(current_sinks)

        # Лист: fanout позволяет подключить все терминалы напрямую.
        # (min_sinks_for_partition < max_fanout никогда не заставит делить группу,
        #  которая и так укладывается в fanout; fanout — жёсткое ограничение.)
        if n <= self.max_fanout:
            for sk in current_sinks:
                self._attach_sink(parent_node, sk)
            return

        # Число подгрупп: 2 (бинарное разбиение), но если дерево рискует превысить
        # max_depth — увеличиваем до max_fanout.
        child_cap = self._capacity(depth + 1)
        k = 2
        while k < self.max_fanout and math.ceil(n / k) > child_cap:
            k += 1

        axis = self._choose_axis(current_sinks, depth)
        other = (lambda s: s.y) if axis == "x" else (lambda s: s.x)
        main = (lambda s: s.x) if axis == "x" else (lambda s: s.y)
        if self.criterion == "balanced_delay":
            # раньше требуемое время прихода — в одну группу, позже — в другую;
            # при равенстве RAT порядок задаёт геометрия
            current_sinks.sort(key=lambda s: (s.required_arrival_time, main(s), other(s)))
        else:
            current_sinks.sort(key=lambda s: (main(s), other(s)))

        groups = self._split_into_k_groups(current_sinks, k, child_cap)

        for group in groups:
            if not group:
                continue
            # Одиночный терминал на последнем допустимом уровне (или без буферов) — напрямую
            if len(group) == 1 and (depth + 1 >= self.max_depth or not self.buffer_insertion):
                self._attach_sink(parent_node, group[0])
                continue

            bx, by = self._calculate_median_position(group)
            node_type = "buffer" if self.buffer_insertion else "branch"
            child_node = ClockNode(
                id=self._next_node_id("BUF" if node_type == "buffer" else "BR"),
                x=bx, y=by, node_type=node_type,
            )
            parent_node.add_child(child_node)
            self._partition_recursive(child_node, group, depth + 1)

    # --------------------------------------------------------------- partition
    def _choose_axis(self, sinks: List[Sink], depth: int) -> str:
        if self.axis_mode == "x":
            return "x"
        if self.axis_mode == "y":
            return "y"
        if self.axis_mode == "alternate":
            return "x" if depth % 2 != 0 else "y"
        # auto: режем вдоль большей стороны охватывающего прямоугольника
        xs = [s.x for s in sinks]
        ys = [s.y for s in sinks]
        return "x" if (max(xs) - min(xs)) >= (max(ys) - min(ys)) else "y"

    def _split_into_k_groups(self, sorted_sinks: List[Sink], k: int, max_group: int) -> List[List[Sink]]:
        """
        Режет отсортированный список на k непрерывных групп с равным весом.
        Вес: ёмкость (balanced_capacitance) либо 1 (balanced_count, balanced_delay).
        Размер группы ограничен сверху (max_group — из max_depth) и снизу
        (min_sinks_for_partition, если это возможно).
        """
        n = len(sorted_sinks)
        k = max(1, min(k, n))

        if self.criterion == "balanced_capacitance":
            weights = [s.capacitance for s in sorted_sinks]
            if sum(weights) <= 0:
                weights = [1.0] * n
        else:
            weights = [1.0] * n

        cum: List[float] = []
        acc = 0.0
        for w in weights:
            acc += w
            cum.append(acc)
        total = cum[-1]

        # Идеальные точки разреза: после i элементов, ближайшие к j/k суммарного веса
        cuts: List[int] = []
        for j in range(1, k):
            target = total * j / k
            i = bisect_left(cum, target)            # cum[i] >= target
            candidates = [c for c in (i, i + 1) if 1 <= c <= n - 1]  # разрез после c элементов
            if not candidates:
                candidates = [min(max(i, 1), n - 1)]
            cuts.append(min(candidates, key=lambda c: abs(cum[c - 1] - target)))

        # Коррекция: размеры групп в пределах [min_size, max_group]
        min_size = max(1, min(self.min_sinks, n // k))
        fixed: List[int] = []
        prev = 0
        for j, c in enumerate(cuts, start=1):
            lo = max(prev + min_size, n - (k - j) * max_group)
            hi = min(prev + max_group, n - (k - j) * min_size)
            c = min(max(c, lo), hi)
            fixed.append(c)
            prev = c

        bounds = [0] + fixed + [n]
        return [sorted_sinks[bounds[i]:bounds[i + 1]] for i in range(k)]

    # ------------------------------------------------------------------ median
    def _calculate_median_position(self, sinks: List[Sink]) -> Tuple[float, float]:
        """Координата узла ветвления по median_type, затем привязка к сетке и габаритам."""
        xs = [s.x for s in sinks]
        ys = [s.y for s in sinks]

        if self.median_type == "centroid":
            total_c = sum(s.capacitance for s in sinks)
            if total_c > 0:
                raw_x = sum(s.x * s.capacitance for s in sinks) / total_c
                raw_y = sum(s.y * s.capacitance for s in sinks) / total_c
            else:
                raw_x, raw_y = sum(xs) / len(xs), sum(ys) / len(ys)
        elif self.median_type == "manhattan_median":
            raw_x, raw_y = statistics.median(xs), statistics.median(ys)
        elif self.median_type == "bbox_center":
            raw_x, raw_y = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        else:  # geometric_median
            raw_x, raw_y = self._weiszfeld(xs, ys)

        return self._snap(raw_x, raw_y)

    @staticmethod
    def _weiszfeld(xs: List[float], ys: List[float], iterations: int = 200, eps: float = 1e-7):
        """Геометрическая медиана (точка с минимальной суммой евклидовых расстояний)."""
        x = sum(xs) / len(xs)
        y = sum(ys) / len(ys)
        for _ in range(iterations):
            num_x = num_y = den = 0.0
            for px, py in zip(xs, ys):
                d = math.hypot(px - x, py - y)
                if d < 1e-12:
                    continue
                num_x += px / d
                num_y += py / d
                den += 1.0 / d
            if den == 0.0:
                break
            nx, ny = num_x / den, num_y / den
            moved = math.hypot(nx - x, ny - y)
            x, y = nx, ny
            if moved < eps:
                break
        return x, y

    def _snap(self, x: float, y: float) -> Tuple[float, float]:
        """Привязка к сетке и ограничение габаритами кристалла."""
        g = self.grid_size
        if g > 0:
            x = round(x / g) * g
            y = round(y / g) * g
            max_x = math.floor(self.chip_width / g) * g if self.chip_width is not None else None
            max_y = math.floor(self.chip_height / g) * g if self.chip_height is not None else None
        else:
            max_x, max_y = self.chip_width, self.chip_height
        if max_x is not None:
            x = min(max(x, 0.0), max_x)
        if max_y is not None:
            y = min(max(y, 0.0), max_y)
        return float(x), float(y)

    # ----------------------------------------------------------------- buffers
    def _assign_buffers(self, root: ClockNode):
        """
        Выбор буферов снизу вверх (когда известна реальная нагрузка каждого буфера,
        включая входные ёмкости нижестоящих буферов).
        """
        def visit(node: ClockNode):
            for ch in node.children:
                visit(ch)
            if node.node_type != "buffer":
                return
            load = 0.0
            for ch in node.children:
                dist = abs(node.x - ch.x) + abs(node.y - ch.y)
                load += dist * self.scale * self.wire_model.unit_capacitance
                if ch.node_type == "sink" and ch.sink_ref:
                    load += ch.sink_ref.capacitance
                elif ch.node_type == "buffer" and ch.buffer_ref:
                    load += ch.buffer_ref.input_capacitance
            node.buffer_ref = self._pick_buffer(load)

        visit(root)

    def _pick_buffer(self, load_ff: float) -> BufferType:
        sel = self.buffer_selection
        if sel == "uniform":
            return self.eligible_buffers[0]            # наименьший доступный
        if sel == "min_delay":
            return min(
                self.eligible_buffers,
                key=lambda b: b.intrinsic_delay + b.output_resistance * load_ff * OHM_FF_TO_NS,
            )
        # size_by_load: самый слабый буфер, который «тянет» эту нагрузку
        for b in self.eligible_buffers:
            if load_ff <= b.drive_strength * self.load_per_drive:
                return b
        return self.eligible_buffers[-1]
