from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class Point:
    """Базовая точка на сетке кристалла"""
    x: float
    y: float

@dataclass
class ClockSource:
    """Источник тактового сигнала (корень дерева)"""
    id: str
    x: float
    y: float
    pin_id: str
    input_capacitance: float
    fixed: bool = True

@dataclass
class Sink:
    """Потребитель тактового сигнала (листья дерева)"""
    id: str
    x: float
    y: float
    pin_id: str
    capacitance: float
    required_arrival_time: float
    fixed: bool = True

@dataclass
class BufferType:
    """Характеристики доступных типов буферов из библиотеки"""
    id: str
    type: str
    input_capacitance: float
    output_resistance: float
    intrinsic_delay: float
    drive_strength: float
    available: bool = True

@dataclass
class WireModel:
    """Параметры межсоединений (проводов)"""
    unit_capacitance: float
    unit_resistance: float
    unit_length: float
    model: str  # "elmore" или "linear"
    linear_delay_per_um: float = 0.001  # нс/мкм, используется только в линейной модели

@dataclass
class ClockNode:
    """Узел дерева синхронизации (может быть точкой ветвления, буфером или sink)"""
    id: str
    x: float
    y: float
    node_type: str  # "source", "branch", "buffer", "sink"
    
    # Исходные объекты (если применимо)
    sink_ref: Optional[Sink] = None
    buffer_ref: Optional[BufferType] = None
    
    # Связи в дереве (используются для обхода графа)
    parent: Optional['ClockNode'] = field(default=None, repr=False)
    children: List['ClockNode'] = field(default_factory=list)
    
    # Электрические характеристики (вычисляются динамически)
    downstream_capacitance: float = 0.0  # Суммарная емкость поддерева ниже узла
    delay_from_source: float = 0.0       # Задержка сигнала от корня до этого узла
    wire_length_to_parent: float = 0.0   # Длина провода до родительского узла (мкм, с учётом змеевика)
    snake_length: float = 0.0            # Добавочная длина провода (змеевик), мкм
    buffer_output_load: float = 0.0      # Нагрузка на выходе буфера (фФ)

    def add_child(self, child_node: 'ClockNode'):
        """Удобный метод для построения связей в дереве"""
        child_node.parent = self
        self.children.append(child_node)
