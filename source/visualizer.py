import matplotlib.pyplot as plt
from models import ClockNode

class CTSVisualizer:
    """Модуль отрисовки топологии сгенерированного дерева синхронизации."""
    
    def __init__(self, chip_width: float, chip_height: float):
        self.chip_width = chip_width
        self.chip_height = chip_height

    def plot_tree(self, root: ClockNode, save_path: str = "data/clock_tree.png"):
        """
        Строит двумерный график дерева синхронизации и сохраняет его в файл.
        Включает подписи идентификаторов для всех элементов: источников, буферов и терминалов.
        """
        # ГАРАНТИЯ ОБНОВЛЕНИЯ: Очищаем старые фигуры и оси перед каждым новым построением
        plt.clf()
        plt.cla()
        
        plt.figure(figsize=(10, 10))
        plt.title("Визуализация дерева синхронизации (Clock Tree Topology)", fontsize=14, fontweight='bold')
        
        # Настройка границ кристалла
        plt.xlim(-50, self.chip_width + 50)
        plt.ylim(-50, self.chip_height + 50)
        plt.axhline(0, color='black', lw=1, ls='--')
        plt.axhline(self.chip_height, color='black', lw=1, ls='--')
        plt.axvline(0, color='black', lw=1, ls='--')
        plt.axvline(self.chip_width, color='black', lw=1, ls='--')
        
        # Списки для группировки объектов (чтобы нарисовать их одним вызовом с общей легендой)
        sources_x, sources_y = [], []
        buffers_x, buffers_y = [], []
        sinks_x, sinks_y = [], []

        # Рекурсивная функция для отрисовки линий связи и сбора координат узлов
        def draw_edges(node: ClockNode):
            if node.node_type == "source":
                sources_x.append(node.x)
                sources_y.append(node.y)
                # Подпись для источника (крупный красный шрифт, чуть выше маркера)
                plt.text(node.x, node.y + 8, node.id, color='red', fontsize=10, 
                         fontweight='bold', ha='center', va='bottom', zorder=7)
                
            elif node.node_type in ["buffer", "branch"]:
                buffers_x.append(node.x)
                buffers_y.append(node.y)
                # Подпись для буферов/бранчей (зеленый аккуратный шрифт, чуть сбоку-сверху)
                label_color = 'darkgreen' if node.node_type == "buffer" else "olive"
                plt.text(node.x + 4, node.y + 4, node.id, color=label_color, fontsize=6, 
                         fontweight='semibold', ha='left', va='bottom', zorder=7)
                
            elif node.node_type == "sink":
                sinks_x.append(node.x)
                sinks_y.append(node.y)
                # Добавляем подпись для терминалов (мелкий синий шрифт, снизу-слева от маркера)
                plt.text(node.x - 3, node.y - 3, node.id, color='darkblue', fontsize=5, 
                         alpha=0.8, ha='right', va='top', zorder=7)

            for child in node.children:
                # Рисуем Манхэттенский провод (сначала по X, потом по Y)
                plt.plot([node.x, child.x], [node.y, node.y], color='gray', alpha=0.6, lw=1.5)
                plt.plot([child.x, child.x], [node.y, child.y], color='gray', alpha=0.6, lw=1.5)
                
                # Рекурсивно идем к детям
                draw_edges(child)

        # Запуск обхода и отрисовки связей
        draw_edges(root)

        # Отрисовка узлов поверх линий
        if sinks_x:
            plt.scatter(sinks_x, sinks_y, color='blue', marker='s', s=10, label='Sinks (Терминалы)', zorder=4)
        if buffers_x:
            plt.scatter(buffers_x, buffers_y, color='green', marker='^', s=30, label='Buffers / Branch (Буферы)', zorder=5)
        if sources_x:
            plt.scatter(sources_x, sources_y, color='red', marker='o', s=50, label='Clock Source (Корень)', zorder=6)

        plt.xlabel("Координата X", fontsize=12)
        plt.ylabel("Координата Y", fontsize=12)
        plt.grid(True, linestyle=':', alpha=0.5)
        plt.legend(loc='upper right', fontsize=11)
        plt.gca().set_aspect('equal', adjustable='box')

        # Сохранение графика (перезаписывает старый файл актуальными данными)
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        print(f"-> График топологии дерева успешно сохранен по пути: {save_path}")
