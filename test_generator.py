import argparse
import json
import math
import os
import random
import time

class CTSTestGenerator:
    """Генератор тестовых сценариев различной сложности для CTS компилятора."""
    
    def __init__(self, seed: int | None = None):
        # Если seed не передан, используем системное время для динамической генерации.
        if seed is None:
            seed = int(time.time() * 1000) % 2**32
        self.seed = seed
        random.seed(seed)
        
        # Общие библиотеки буферов и параметры физики (согласно ТЗ)
        self.buffers = [
            {"id": "BUF_STD", "type": "standard", "input_capacitance": 0.5, "output_resistance": 50.0, "intrinsic_delay": 0.15, "drive_strength": 1.0, "available": True},
            {"id": "BUF_MID", "type": "mid", "input_capacitance": 1.0, "output_resistance": 25.0, "intrinsic_delay": 0.20, "drive_strength": 2.0, "available": True},
            {"id": "BUF_LARGE", "type": "large", "input_capacitance": 2.0, "output_resistance": 10.0, "intrinsic_delay": 0.30, "drive_strength": 4.0, "available": True}
        ]
        self.wire_model = {
            "unit_capacitance": 0.2,
            "unit_resistance": 0.05,
            "unit_length": 1.0,
            "model": "elmore"
        }
        self.optimization = {
            "objective": "min_skew",
            "secondary_objective": "min_power",
            "max_iterations": 20,
            "convergence_tol": 1e-4,
            "skew_target": 0.05,
            "power_weight": 0.1,
            "delay_weight": 0.9
        }

    # ------------------------------------------------------------------
    # Случайные (но заведомо допустимые) параметры tree_params / optimization
    # ------------------------------------------------------------------
    def random_tree_params(self, num_sinks: int) -> dict:
        """Случайный набор tree_params; дерево гарантированно вмещает num_sinks терминалов."""
        max_fanout = random.choice([2, 3, 4, 4, 4, 5, 6, 8])
        # минимальная глубина, при которой max_fanout^(depth-1) >= num_sinks
        min_depth = math.ceil(math.log(max(num_sinks, 2)) / math.log(max_fanout)) + 1
        max_depth = min(min_depth + random.randint(0, 3), 16)
        return {
            "algorithm": "median_partition",
            "max_fanout": max_fanout,
            "min_sinks_for_partition": random.choice([2, 2, 3]),
            "max_depth": max_depth,
            "partition_criterion": random.choice(["balanced_capacitance", "balanced_count", "balanced_delay"]),
            "partition_axis": random.choice(["auto", "x", "y", "alternate"]),
            "median_type": random.choice(["geometric_median", "centroid", "manhattan_median"]),
            "buffer_insertion": random.random() < 0.9,
            "buffer_selection": random.choice(["size_by_load", "uniform", "min_delay"]),
            # в библиотеке буферы с входной ёмкостью 0.5 / 1.0 / 2.0 — порог не должен выбить их все
            "min_buffer_input_cap": random.choice([0.0, 0.5, 0.5, 1.0]),
            "snaking_allowed": random.random() < 0.8,
            "wire_snaking_limit": random.choice([0.05, 0.1, 0.2, 0.3, 0.5]),
            "obstacle_aware": random.random() < 0.1,
        }

    def random_optimization(self) -> dict:
        """Случайный набор optimization."""
        objective = random.choice(["min_skew", "min_skew", "min_delay", "min_power"])
        secondary = random.choice([o for o in ("min_skew", "min_delay", "min_power") if o != objective] + [None])
        delay_weight = round(random.uniform(0.5, 0.95), 2)
        return {
            "objective": objective,
            "secondary_objective": secondary,
            "max_iterations": random.choice([5, 10, 20, 30]),
            "convergence_tol": random.choice([1e-3, 1e-4, 1e-5]),
            "skew_target": random.choice([0.02, 0.05, 0.05, 0.1]),
            "power_weight": round(1.0 - delay_weight, 2),
            "delay_weight": delay_weight,
        }

    def generate_case(self, filename: str, num_sinks: int, chip_size: int = 2000, grid_size: int = 10,
                      vary_params: bool = True):
        """Создает и сохраняет один тестовый JSON файл; координаты терминалов кратны шагу сетки.
        vary_params=True — tree_params и optimization случайные; False — фиксированные значения из ТЗ."""
        
        # Задаем параметры кристалла
        design = {
            "chip_width": chip_size,
            "chip_height": chip_size,
            "grid_size": grid_size,
            # Координаты терминалов всегда кратны сетке (см. ТЗ)
            "snap_terminals_to_grid": True
        }

        # Источник тактовой частоты по центру верхней границы кристалла (кратно сетке)
        clock_source = {
            "id": "CLK_SRC",
            "x": float((chip_size // 2) // grid_size * grid_size),
            "y": float(chip_size // grid_size * grid_size),
            "pin_id": "CLK_PIN",
            "input_capacitance": 0.0,
            "fixed": True
        }
        
        # Алгоритмические параметры сборки дерева
        fixed_tree_params = {
            "algorithm": "median_partition",
            "max_fanout": 4,
            "min_sinks_for_partition": 2,
            "max_depth": 12 if num_sinks > 500 else 10,
            "partition_criterion": "balanced_capacitance",
            "partition_axis": "auto",
            "median_type": "geometric_median",
            "buffer_insertion": True,
            "buffer_selection": "size_by_load",
            "min_buffer_input_cap": 0.5,
            "snaking_allowed": True,
            "wire_snaking_limit": 0.2,
            "obstacle_aware": False
        }
        tree_params = self.random_tree_params(num_sinks) if vary_params else fixed_tree_params
        optimization = self.random_optimization() if vary_params else self.optimization
        
        # Генерация случайных потребителей (Sinks); координаты кратны шагу сетки
        sinks = []
        steps = chip_size // grid_size
        for i in range(1, num_sinks + 1):
            # Любой узел сетки внутри кристалла
            x_coord = random.randint(0, steps) * grid_size
            y_coord = random.randint(0, steps) * grid_size
            
            # Случайная емкость затвора триггера от 2.0 до 6.0 фФ
            cap = round(random.uniform(2.0, 6.0), 1)
            
            sinks.append({
                "id": f"S{i}",
                "x": float(x_coord),
                "y": float(y_coord),
                "pin_id": f"S{i}_PIN",
                "capacitance": cap,
                "required_arrival_time": 3.0 if num_sinks > 500 else 2.5,
                "fixed": True
            })
            
        # Формируем итоговую структуру JSON
        config_data = {
            "design": design,
            "clock_source": clock_source,
            "sinks": sinks,
            "buffers": self.buffers,
            "wire_model": self.wire_model,
            "tree_params": tree_params,
            "optimization": optimization
        }

        
        # Убедимся, что папка существует, и запишем файл
        os.makedirs("tests", exist_ok=True)
        filepath = os.path.join("tests", filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
            
        print(f"-> УСПЕХ: сгенерирован тест {filepath} ({num_sinks} терминалов) со случайными координатами.")
        if vary_params:
            tp, op = tree_params, optimization
            print(f"   tree: fanout={tp['max_fanout']} depth={tp['max_depth']} {tp['partition_criterion']}/{tp['partition_axis']}/"
                  f"{tp['median_type']} buf={tp['buffer_insertion']}/{tp['buffer_selection']}")
            print(f"   opt : {op['objective']}+{op['secondary_objective']} iter={op['max_iterations']} "
                  f"skew_target={op['skew_target']} w(d/p)={op['delay_weight']}/{op['power_weight']}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Генератор тестов CTS")
    ap.add_argument("--seed", type=int, default=None, help="seed (по умолчанию — от времени)")
    ap.add_argument("--fixed", action="store_true",
                    help="не генерировать tree_params/optimization случайно, взять значения из примера ТЗ")
    args = ap.parse_args()

    generator = CTSTestGenerator(seed=args.seed)
    vary = not args.fixed

    print("=" * 40)
    print(f"ГЕНЕРАЦИЯ СЛУЧАЙНЫХ ТЕСТОВЫХ НАБОРОВ CTS (seed={generator.seed}, "
          f"tree_params/optimization: {'случайные' if vary else 'из примера ТЗ'})")
    print("=" * 40)

    # Профили сложности согласно спецификации ТЗ
    generator.generate_case("test_easy.json", num_sinks=10, vary_params=vary)
    generator.generate_case("test_medium.json", num_sinks=100, vary_params=vary)
    generator.generate_case("test_hard.json", num_sinks=500, vary_params=vary)
    generator.generate_case("test_stress.json", num_sinks=1000, vary_params=vary)

    print("=" * 40)
