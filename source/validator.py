import math
from typing import Dict, List, Set, Tuple
from models import ClockNode, BufferType


class CTSValidator:
    """
    Верификация дерева синхронизации:
      - все sink'и подключены, нет циклов, граф — дерево (N узлов, N-1 рёбер);
      - узлы в пределах кристалла и (для буферов/точек ветвления) на сетке;
      - fanout и глубина не превышены;
      - буферы — из списка доступных и с input_capacitance >= min_buffer_input_cap;
      - задержка рассчитана для каждого sink'а; timing compliance (предупреждения).
    """

    def __init__(
        self,
        max_fanout: int,
        max_depth: int,
        chip_width: float,
        chip_height: float,
        grid_size: float,
        available_buffers: Dict[str, BufferType],
        min_buffer_input_cap: float = 0.0,
    ):
        self.max_fanout = max_fanout
        self.max_depth = max_depth
        self.chip_width = chip_width
        self.chip_height = chip_height
        self.grid_size = grid_size
        self.available_buffers = available_buffers
        self.min_buffer_input_cap = min_buffer_input_cap

        self.errors: List[str] = []
        self.warnings: List[str] = []
        self._nodes: List[ClockNode] = []

    def validate(self, root: ClockNode, expected_sinks_count: int,
                 sink_delays: Dict[str, float]) -> Tuple[bool, List[str], List[str]]:
        self.errors.clear()
        self.warnings.clear()
        self._nodes = []

        if not root:
            self.errors.append("Дерево синхронизации пустое (отсутствует корень).")
            return False, self.errors, self.warnings

        visited_nodes: Set[str] = set()
        connected_sinks: Set[str] = set()
        edges = [0]

        self._traverse_and_verify(root, visited_nodes, connected_sinks, edges, current_depth=1)
        self._check_overlaps()
        self._check_terminal_grid()

        if len(connected_sinks) != expected_sinks_count:
            self.errors.append(
                f"Нарушена связность! Подключено sink'ов: {len(connected_sinks)} из ожидаемых {expected_sinks_count}."
            )

        # Граф — дерево: N узлов, N-1 рёбер (одна компонента связности от корня)
        if edges[0] != len(visited_nodes) - 1:
            self.errors.append(
                f"Граф не является деревом: узлов {len(visited_nodes)}, рёбер {edges[0]} (ожидалось {len(visited_nodes) - 1})."
            )

        # Задержки рассчитаны для каждого sink'а и конечны
        missing = connected_sinks - set(sink_delays)
        if missing:
            self.errors.append(f"Не рассчитана задержка для sink'ов: {', '.join(sorted(missing)[:10])}.")
        bad = [sid for sid, d in sink_delays.items() if not math.isfinite(d)]
        if bad:
            self.errors.append(f"Некорректная (нечисловая) задержка у sink'ов: {', '.join(sorted(bad)[:10])}.")

        return len(self.errors) == 0, self.errors, self.warnings

    def _on_grid(self, value: float) -> bool:
        r = value % self.grid_size
        return math.isclose(r, 0, abs_tol=1e-5) or math.isclose(r, self.grid_size, abs_tol=1e-5)

    def _traverse_and_verify(self, node: ClockNode, visited: Set[str], connected_sinks: Set[str],
                             edges: List[int], current_depth: int):
        if node.id in visited:
            self.errors.append(f"Обнаружен цикл в дереве! Узел '{node.id}' посещен повторно.")
            return
        visited.add(node.id)
        self._nodes.append(node)

        # 1. Границы кристалла
        if not (0 <= node.x <= self.chip_width) or not (0 <= node.y <= self.chip_height):
            self.errors.append(
                f"Узел '{node.id}' ({node.node_type}) вышел за границы кристалла: ({node.x}, {node.y})."
            )

        # 2. Привязка к сетке — только для узлов, которые строит алгоритм
        #    (источник и sink'и зафиксированы и не двигаются)
        if self.grid_size > 0 and node.node_type in ("buffer", "branch"):
            if not (self._on_grid(node.x) and self._on_grid(node.y)):
                self.errors.append(
                    f"Узел '{node.id}' ({node.node_type}) нарушил привязку к сетке! "
                    f"Координаты: ({node.x}, {node.y}), Шаг сетки: {self.grid_size}."
                )

        # 3. Глубина
        if current_depth > self.max_depth:
            self.errors.append(
                f"Превышена максимальная глубина дерева! Узел '{node.id}' находится на уровне {current_depth} (max: {self.max_depth})."
            )

        # 4. Fanout
        if len(node.children) > self.max_fanout:
            self.errors.append(
                f"Превышено максимальное ветвление (fanout) для узла '{node.id}': "
                f"потомков {len(node.children)} (max: {self.max_fanout})."
            )

        # 5. Библиотека буферов
        if node.node_type == "buffer":
            if not node.buffer_ref:
                self.errors.append(f"Узел '{node.id}' заявлен как буфер, но не имеет ссылки на параметры BufferType.")
            elif node.buffer_ref.id not in self.available_buffers:
                self.errors.append(
                    f"Буфер '{node.id}' использует недоступный или отсутствующий в библиотеке тип '{node.buffer_ref.id}'."
                )
            elif node.buffer_ref.input_capacitance < self.min_buffer_input_cap:
                self.errors.append(
                    f"Буфер '{node.id}' ({node.buffer_ref.id}): input_capacitance "
                    f"{node.buffer_ref.input_capacitance} < min_buffer_input_cap {self.min_buffer_input_cap}."
                )

        # 6. Timing compliance
        if node.node_type == "sink" and node.sink_ref:
            connected_sinks.add(node.sink_ref.id)
            if node.delay_from_source > node.sink_ref.required_arrival_time:
                self.warnings.append(
                    f"Нарушение таймингов на '{node.sink_ref.id}': "
                    f"задержка {node.delay_from_source:.3f} > требуется {node.sink_ref.required_arrival_time}."
                )

        for child in node.children:
            edges[0] += 1
            if child.parent is not node:
                self.errors.append(f"Нарушена иерархическая структура! Узел '{child.id}' указывает на неверного родителя.")
            self._traverse_and_verify(child, visited, connected_sinks, edges, current_depth + 1)

    def _check_overlaps(self):
        """
        Наложение узлов: буфер/точка ветвления не может занимать ту же координату,
        что терминал (регистр или источник) — это физически недопустимо.
        Совпадения буферов между собой и терминалов между собой — предупреждения.
        """
        by_pos: Dict[Tuple[float, float], List[ClockNode]] = {}
        for n in self._nodes:
            by_pos.setdefault((round(n.x, 6), round(n.y, 6)), []).append(n)

        for (x, y), nodes in by_pos.items():
            if len(nodes) < 2:
                continue
            cell_nodes = [n for n in nodes if n.node_type in ("buffer", "branch")]
            sink_nodes = [n for n in nodes if n.node_type == "sink"]
            source_nodes = [n for n in nodes if n.node_type == "source"]

            if cell_nodes and (sink_nodes or source_nodes):
                self.errors.append(
                    f"Наложение буфера и терминала в точке ({x}, {y}): "
                    + ", ".join(f"{n.id} ({n.node_type})" for n in nodes) + "."
                )
            if len(cell_nodes) > 1:
                self.warnings.append(
                    f"Совпадение координат буферов/точек ветвления в точке ({x}, {y}): "
                    + ", ".join(n.id for n in cell_nodes) + "."
                )
            if len(sink_nodes) > 1:
                self.warnings.append(
                    f"Терминалы совпадают по координатам в точке ({x}, {y}): "
                    + ", ".join(n.id for n in sink_nodes) + "."
                )

    def _check_terminal_grid(self):
        """Координаты терминалов (источник, sink'и) должны быть кратны шагу сетки."""
        if self.grid_size <= 0:
            return
        for n in self._nodes:
            if n.node_type not in ("source", "sink"):
                continue
            if not (self._on_grid(n.x) and self._on_grid(n.y)):
                self.warnings.append(
                    f"Терминал '{n.id}' ({n.node_type}) не кратен сетке: "
                    f"({n.x}, {n.y}), шаг сетки {self.grid_size}. "
                    f"Включите design.snap_terminals_to_grid для привязки к сетке."
                )
