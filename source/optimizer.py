import math
from typing import Dict, Any, Tuple, List, Optional
from models import ClockNode, WireModel, BufferType
from delay_calc import DelayCalculator, OHM_FF_TO_NS

_EPS = 1e-12


class CTSOptimizer:
    """
    Постоптимизация дерева синхронизации.

    optimization:   objective, secondary_objective, max_iterations, convergence_tol,
                    skew_target, power_weight, delay_weight
    tree_params:    snaking_allowed, wire_snaking_limit, buffer_selection

    Целевая функция J строится из метрик, выбранных в objective / secondary_objective:
      * delay-метрика D: skew (min_skew) или максимальная задержка (min_delay);
      * мощность P: суммарная переключаемая ёмкость дерева, фФ (min_power).
    Если выбраны и delay-метрика, и мощность:  J = delay_weight * D/D0 + power_weight * P/P0
    (D0, P0 — значения до оптимизации). Если выбран один тип метрики — J = эта метрика.

    Ходы оптимизации (принимаются, только если J уменьшилась):
      1. смена типа буфера (кроме buffer_selection = "uniform");
      2. змеевик (wire snaking) на быстрых ветвях — если min_skew входит в цели
         (objective или secondary): при основной цели skew — по снижению J,
         при вторичной — по снижению skew без ухудшения max_delay.
    """

    def __init__(
        self,
        wire_model: WireModel,
        opt_params: Dict[str, Any],
        tree_params: Optional[Dict[str, Any]] = None,
        candidate_buffers: Optional[List[BufferType]] = None,
    ):
        tree_params = tree_params or {}
        self.wire_model = wire_model
        self.params = opt_params

        self.objective = opt_params.get("objective", "min_skew")
        self.secondary = opt_params.get("secondary_objective")
        self.max_iterations = int(opt_params.get("max_iterations", 20))
        self.convergence_tol = float(opt_params.get("convergence_tol", 1e-4))
        self.skew_target = float(opt_params.get("skew_target", 0.05))
        self.power_weight = float(opt_params.get("power_weight", 0.1))
        self.delay_weight = float(opt_params.get("delay_weight", 0.9))

        # snaking_* лежат в tree_params (по заданию)
        self.snaking_allowed = bool(tree_params.get("snaking_allowed", True))
        self.snake_limit = float(tree_params.get("wire_snaking_limit", 0.2))

        # Какие метрики входят в J
        objectives = [o for o in (self.objective, self.secondary) if o]
        delay_objs = [o for o in objectives if o in ("min_skew", "min_delay")]
        self.delay_metric: Optional[str] = delay_objs[0] if delay_objs else None  # приоритет у objective
        self.use_power = "min_power" in objectives
        # min_skew может быть и основной, и вторичной целью: если он указан в любой
        # из целей, оптимизатор дополнительно целится в skew_target (см. _accept_snake).
        self.skew_is_goal = "min_skew" in objectives

        # Смена буферов запрещена при buffer_selection = uniform
        if tree_params.get("buffer_selection", "size_by_load") == "uniform":
            self.candidates: List[BufferType] = []
        else:
            self.candidates = sorted(candidate_buffers or [], key=lambda b: (b.drive_strength, b.id))

        self.calc = DelayCalculator(wire_model)

        # Служебное
        self._nodes: List[ClockNode] = []
        self._buffer_nodes: List[ClockNode] = []
        self._ref_d = 1.0
        self._ref_p = 1.0
        self.last_delays: Dict[str, float] = {}

        # Статистика для отчёта
        self.initial: Dict[str, float] = {}
        self.final: Dict[str, float] = {}
        self.iterations = 0
        self.buffers_resized = 0
        self.edges_snaked = 0

    # ------------------------------------------------------------ metrics/cost
    def _evaluate(self, root: ClockNode) -> Dict[str, float]:
        delays, skew = self.calc.calculate(root)
        self.last_delays = delays
        values = list(delays.values())
        return {
            "max_delay": max(values) if values else 0.0,
            "min_delay": min(values) if values else 0.0,
            "skew": skew,
            "power": self.calc.total_capacitance(root),
        }

    def _delay_term(self, m: Dict[str, float]) -> float:
        return m["max_delay"] if self.delay_metric == "min_delay" else m["skew"]

    def _cost(self, m: Dict[str, float]) -> float:
        d = self._delay_term(m) / self._ref_d if self.delay_metric else 0.0
        p = m["power"] / self._ref_p if self.use_power else 0.0
        if self.delay_metric and self.use_power:
            return self.delay_weight * d + self.power_weight * p
        return d + p

    def _convergence_value(self, m: Dict[str, float]) -> float:
        # По заданию — изменение skew; для остальных целей — изменение J
        return m["skew"] if self.delay_metric == "min_skew" else self._cost(m)

    def _skew_goal_reached(self, m: Dict[str, float]) -> bool:
        return self.delay_metric == "min_skew" and m["skew"] <= self.skew_target

    def _accept_snake(self, m: Dict[str, float], m2: Dict[str, float],
                      cost: float, c2: float) -> bool:
        """Принимает удлинение проводов.
        Если skew — основная delay-метрика, судим по снижению J.
        Если skew — вторичная цель, принимаем, когда выигрыш по skew больше проигрыша
        по основной задержке (max_delay): так приоритет objective в J сохраняется."""
        if self.delay_metric == "min_skew":
            return c2 < cost - _EPS
        d_skew = m["skew"] - m2["skew"]
        d_delay = m2["max_delay"] - m["max_delay"]
        return d_skew > _EPS and d_delay <= d_skew

    # ---------------------------------------------------------------- optimize
    def optimize(self, root: ClockNode) -> Tuple[Dict[str, float], float]:
        """Возвращает (задержки sink'ов, итоговый skew)."""
        m = self._evaluate(root)
        self.initial = dict(m)
        self.final = dict(m)
        if not self.last_delays:
            return self.last_delays, 0.0

        self._collect_nodes(root)
        self._ref_d = m["skew"] if (self.delay_metric == "min_skew" and m["skew"] > _EPS) else (
            m["max_delay"] if self.delay_metric == "min_delay" and m["max_delay"] > _EPS else 1.0)
        self._ref_p = m["power"] if m["power"] > _EPS else 1.0

        if self._skew_goal_reached(m):
            return self.last_delays, m["skew"]

        cost = self._cost(m)
        for it in range(self.max_iterations):
            self.iterations = it + 1
            before = self._convergence_value(m)
            accepted = False

            # Ход 1: смена типов буферов
            if self.candidates and self._buffer_nodes:
                changed = self._resize_pass(root, cost)
                m = self._evaluate(root)
                cost = self._cost(m)
                if changed:
                    accepted = True
                    self.buffers_resized += changed

            # Ход 2: змеевик — если skew входит в цели оптимизации и не достигнут
            if self.snaking_allowed and self.skew_is_goal and m["skew"] > self.skew_target:
                moved = False
                for factor in (1.0, 0.5, 0.25):
                    snap = self._snapshot_snakes()
                    n = self._snake_pass(root, m["max_delay"], factor)
                    if n == 0:
                        break
                    m2 = self._evaluate(root)
                    c2 = self._cost(m2)
                    if self._accept_snake(m, m2, cost, c2):
                        m, cost = m2, c2
                        accepted = moved = True
                        self.edges_snaked += n
                        break
                    self._restore_snakes(snap)
                if not moved:
                    m = self._evaluate(root)   # вернуть расчётные поля узлов в согласованное состояние
                    cost = self._cost(m)

            if self._skew_goal_reached(m):
                break
            if not accepted:
                break
            if abs(before - self._convergence_value(m)) < self.convergence_tol:
                break

        self.final = dict(m)
        return self.last_delays, m["skew"]

    # ---------------------------------------------------------------- helpers
    def _collect_nodes(self, root: ClockNode):
        self._nodes = []
        stack = [root]
        while stack:
            n = stack.pop()
            self._nodes.append(n)
            stack.extend(n.children)
        self._buffer_nodes = [n for n in self._nodes if n.node_type == "buffer" and n.buffer_ref]

    def _snapshot_snakes(self) -> List[float]:
        return [n.snake_length for n in self._nodes]

    def _restore_snakes(self, snap: List[float]):
        for n, v in zip(self._nodes, snap):
            n.snake_length = v

    # ----------------------------------------------------------------- resizing
    def _resize_pass(self, root: ClockNode, cost: float) -> int:
        """Жадно перебирает типы буферов у каждого буфера; оставляет лучший по J."""
        changed = 0
        for node in self._buffer_nodes:
            original = node.buffer_ref
            best_ref, best_cost = original, cost
            for cand in self.candidates:
                if cand.id == original.id:
                    continue
                node.buffer_ref = cand
                c = self._cost(self._evaluate(root))
                if c < best_cost - _EPS:
                    best_ref, best_cost = cand, c
            node.buffer_ref = best_ref
            if best_ref.id != original.id:
                changed += 1
                cost = best_cost
        return changed

    # ------------------------------------------------------------------ snaking
    def _subtree_max_delay(self, node: ClockNode, out: Dict[int, float]) -> float:
        if not node.children:
            out[id(node)] = node.delay_from_source
        else:
            out[id(node)] = max(self._subtree_max_delay(ch, out) for ch in node.children)
        return out[id(node)]

    def _snake_pass(self, root: ClockNode, target: float, factor: float) -> int:
        """
        Сверху вниз удлиняет провода к «быстрым» поддеревьям так, чтобы их самый
        медленный sink дошёл до target. Накопленный сдвиг задержки предков учитывается.
        """
        sub: Dict[int, float] = {}
        self._subtree_max_delay(root, sub)
        count = 0

        def visit(node: ClockNode, shift: float):
            nonlocal count
            if node.node_type == "buffer" and node.buffer_ref:
                r_drv = node.buffer_ref.output_resistance
            else:
                r_drv = 0.0
            for child in node.children:
                deficit = (target - (sub[id(child)] + shift)) * factor
                new_shift = shift
                if deficit > 1e-9:
                    gained = self._extend_edge(child, r_drv, deficit)
                    if gained > 0.0:
                        count += 1
                        new_shift += gained
                visit(child, new_shift)

        visit(root, 0.0)
        return count

    def _extend_edge(self, child: ClockNode, r_driver: float, deficit: float) -> float:
        """
        Удлиняет провод к child, чтобы добавить ~deficit нс. Возвращает фактически добавленную задержку.
        Elmore: a*dL^2 + b*dL = deficit, a = K*r*c/2, b = K*(r*(c*L + C_load) + R_drv*c).
        """
        length = child.wire_length_to_parent
        geometric = length - child.snake_length
        room = self.snake_limit * geometric - child.snake_length   # wire_snaking_limit — доля от длины
        if room <= 1e-9:
            return 0.0

        r = self.wire_model.unit_resistance
        c = self.wire_model.unit_capacitance
        c_load = child.downstream_capacitance
        if self.wire_model.model.lower() == "elmore":
            a = 0.5 * r * c * OHM_FF_TO_NS
            b = OHM_FF_TO_NS * (r * (c * length + c_load) + r_driver * c)
        else:
            a = 0.0
            b = self.wire_model.linear_delay_per_um + OHM_FF_TO_NS * r_driver * c

        if a > 0.0:
            d_len = (-b + math.sqrt(b * b + 4.0 * a * deficit)) / (2.0 * a)
        elif b > 0.0:
            d_len = deficit / b
        else:
            return 0.0

        d_len = min(d_len, room)
        child.snake_length += d_len
        child.wire_length_to_parent += d_len
        return a * d_len * d_len + b * d_len
