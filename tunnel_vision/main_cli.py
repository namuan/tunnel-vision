import logging
import sys

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget

# Set up logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("TunnelVision")

# Import macOS-specific modules
try:
    # Import CoreGraphics for events
    from Quartz.CoreGraphics import (
        CGEventCreateScrollWheelEvent,
        CGEventPost,
        kCGHIDEventTap,
        kCGScrollEventUnitPixel,
    )

    MACOS_MODULES_AVAILABLE = True
except ImportError as e:
    MACOS_MODULES_AVAILABLE = False
    print(f"Failed to import macOS modules: {e}. Try: pip install pyobjc")


class FocusAreaWidget(QWidget):
    """A separate widget just for the transparent focus area"""

    clicked = pyqtSignal()

    def __init__(self, rect, parent=None):
        super().__init__(parent)
        self.focus_rect = rect
        self.setGeometry(rect)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setStyleSheet("background-color: transparent;")

    def mousePressEvent(self, event):
        self.clicked.emit()
        event.accept()  # Prevent event propagation


class FocusOverlayWidget(QWidget):
    def __init__(self):
        super().__init__()

        # Original window flags
        self.base_flags = Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(self.base_flags | Qt.WindowType.WindowStaysOnTopHint)
        self.always_on_top = True

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Define the focus rectangle with rounded corners
        self.focus_rect = QRect(200, 150, 800, 500)
        self.corner_radius = 20  # radius for rounded corners

        # Variables to manage dragging of the corners
        self.dragging = False
        self.drag_corner = None
        self.drag_start_pos = None
        self.original_rect = QRect()

        # The size in pixels around a corner that will be sensitive to dragging
        self.hit_threshold = 14
        self.handle_size = 10  # Size of the visible corner handles

        # Auto-scroll variables
        self.auto_scrolling = False
        # Remove the scroll_timer and just keep smooth_scroll_timer
        self.smooth_scroll_timer = QTimer(self)
        self.smooth_scroll_timer.timeout.connect(self.perform_smooth_scroll)
        self.scroll_speed = -1  # default lines per scroll
        self.scroll_interval = 10000  # milliseconds
        self.smooth_scroll_steps = 50  # Number of steps for smooth scrolling
        self.current_smooth_step = 0

        # Status display
        self.last_action = "Ready"

        # Create a separate widget for the focus area
        self.focus_area = FocusAreaWidget(self.focus_rect, self)

        # Set cursor for corners
        self.setMouseTracking(True)  # Enable mouse tracking for cursor changes

    def update_focus_area_geometry(self):
        """Update the focus area widget to match the focus rectangle"""
        self.focus_area.setGeometry(self.focus_rect)
        self.focus_area.focus_rect = self.focus_rect

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Fill the entire window with a semi-transparent dark overlay
        overlay_color = QColor(0, 0, 0, 200)
        painter.fillRect(self.rect(), overlay_color)

        # Set the composition mode to clear so that the focus rectangle becomes transparent
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)

        # Create a path with a rounded rectangle for the focus area
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.focus_rect), self.corner_radius, self.corner_radius)
        painter.fillPath(path, QColor(0, 0, 0, 0))

        # Draw an outline around the focus rectangle
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # Use different colors based on state
        border_pen = (
            QPen(QColor(155, 206, 223), 3) if self.auto_scrolling else QPen(QColor(170, 170, 190), 2)
        )  # Muted blue-gray for normal

        painter.setPen(border_pen)
        painter.drawRoundedRect(self.focus_rect, self.corner_radius, self.corner_radius)

        # Draw corner handles
        handle_color = QColor(255, 255, 255)
        if self.auto_scrolling:
            handle_color = QColor(155, 206, 223)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(handle_color)

        # Draw the corner and middle handles
        corners = {
            "top-left": self.focus_rect.topLeft(),
            "top-right": self.focus_rect.topRight(),
            "bottom-left": self.focus_rect.bottomLeft(),
            "bottom-right": self.focus_rect.bottomRight(),
            "top-middle": QPoint(self.focus_rect.center().x(), self.focus_rect.top()),
        }

        for _, point in corners.items():
            # Draw circular handle centered on corner
            handle_rect = QRectF(
                point.x() - self.handle_size / 2, point.y() - self.handle_size / 2, self.handle_size, self.handle_size
            )
            painter.drawEllipse(handle_rect)

        # Merge keyboard shortcuts and current status
        info_display = [
            "ESC: Exit",
            f"SPACE: Scroll Direction [{('DOWN' if self.scroll_speed < 0 else 'UP')}]",
            f"+/-: Scroll Speed [{abs(self.scroll_speed)}]",
            f"S: Auto-Scroll [{('ON' if self.auto_scrolling else 'OFF')}]",
            f"T: Always on Top [{('ON' if self.always_on_top else 'OFF')}]",
        ]

        # Draw merged info
        debug_y = self.height() - 140
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        for info_text in info_display:
            painter.drawText(10, debug_y, info_text)
            debug_y += 20

    def _get_mouse_position(self, event):
        """Get mouse position regardless of Qt version"""
        return event.position().toPoint() if hasattr(event, "position") else event.pos()

    def _update_rect_dimensions(self, rect, dx, dy, corner):
        """Update rectangle dimensions based on drag direction"""
        min_width, min_height = 100, 100
        new_dims = {"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()}

        if corner == "top-middle":
            new_dims["x"] += dx
            new_dims["y"] += dy
        else:
            # Handle horizontal changes
            if "left" in corner:
                new_dims["x"] += dx
                new_dims["width"] -= dx
            elif "right" in corner:
                new_dims["width"] += dx

            # Handle vertical changes
            if "top" in corner:
                new_dims["y"] += dy
                new_dims["height"] -= dy
            elif "bottom" in corner:
                new_dims["height"] += dy

            # Enforce minimum dimensions
            if new_dims["width"] < min_width:
                if "left" in corner:
                    new_dims["x"] = rect.x() + (rect.width() - min_width)
                new_dims["width"] = min_width

            if new_dims["height"] < min_height:
                if "top" in corner:
                    new_dims["y"] = rect.y() + (rect.height() - min_height)
                new_dims["height"] = min_height

        return QRect(new_dims["x"], new_dims["y"], new_dims["width"], new_dims["height"])

    def _update_cursor_for_corner(self, corner):
        """Set appropriate cursor based on corner"""
        if corner == "top-middle":
            self.setCursor(Qt.CursorShape.SizeAllCursor)  # Changed to movement cursor
        elif corner in ("top-left", "bottom-right"):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)

    def mouseMoveEvent(self, event):
        """Handle mouse movement for corner dragging and cursor changing"""
        pos = self._get_mouse_position(event)

        if self.dragging:
            dx = pos.x() - self.drag_start_pos.x()
            dy = pos.y() - self.drag_start_pos.y()
            self.focus_rect = self._update_rect_dimensions(self.original_rect, dx, dy, self.drag_corner)
            self.update()
        else:
            corners = {
                "top-left": self.focus_rect.topLeft(),
                "top-right": self.focus_rect.topRight(),
                "bottom-left": self.focus_rect.bottomLeft(),
                "bottom-right": self.focus_rect.bottomRight(),
                "top-middle": QPoint(self.focus_rect.center().x(), self.focus_rect.top()),
            }

            for corner, point in corners.items():
                if abs(point.x() - pos.x()) <= self.hit_threshold and abs(point.y() - pos.y()) <= self.hit_threshold:
                    self._update_cursor_for_corner(corner)
                    break
            else:  # No corner detected
                self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        """Handle mouse press events for corner dragging only"""
        pos = self._get_mouse_position(event)

        # Check if near corner for dragging
        corners = {
            "top-left": self.focus_rect.topLeft(),
            "top-right": self.focus_rect.topRight(),
            "bottom-left": self.focus_rect.bottomLeft(),
            "bottom-right": self.focus_rect.bottomRight(),
            "top-middle": QPoint(self.focus_rect.center().x(), self.focus_rect.top()),
        }

        for corner, point in corners.items():
            if abs(point.x() - pos.x()) <= self.hit_threshold and abs(point.y() - pos.y()) <= self.hit_threshold:
                self.dragging = True
                self.drag_corner = corner
                self.drag_start_pos = pos
                self.original_rect = QRect(self.focus_rect)
                self.last_action = f"Dragging {corner}"

                # Hide the focus area widget during dragging
                self.focus_area.hide()
                return

    def toggle_auto_scroll(self):
        """Toggle auto-scrolling state"""
        self.auto_scrolling = not self.auto_scrolling

        if self.auto_scrolling:
            self.last_action = "Started auto-scrolling"

            # Make the focus area widget transparent to mouse events
            # This allows clicks to pass through to underlying applications
            self.focus_area.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

            # Reset step counter and start smooth scrolling
            self.current_smooth_step = 0

            # Calculate time between smooth scrolls
            smooth_interval = int((self.scroll_interval) / self.smooth_scroll_steps)
            self.smooth_scroll_timer.start(smooth_interval)
        else:
            self.last_action = "Stopped auto-scrolling"
            self.smooth_scroll_timer.stop()

            # Make the focus area widget non-transparent to capture clicks again
            self.focus_area.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.update()

    def perform_smooth_scroll(self):
        """Perform a small portion of the scroll for smoother animation"""
        if not self.auto_scrolling or not MACOS_MODULES_AVAILABLE:
            self.smooth_scroll_timer.stop()
            return

        try:
            # Calculate pixels to scroll based on speed
            pixels_per_step = abs(self.scroll_speed) * 1  # 5 pixels per speed unit

            # Create a scroll wheel event with pixel-based scrolling
            scroll_event = CGEventCreateScrollWheelEvent(
                None,  # No source
                kCGScrollEventUnitPixel,
                1,  # Number of wheels (1 for vertical only)
                pixels_per_step if self.scroll_speed > 0 else -pixels_per_step,
            )

            # Post the event
            CGEventPost(kCGHIDEventTap, scroll_event)

            # Increment step counter
            self.current_smooth_step += 1

            # When we reach the end of the steps, reset counter and continue
            if self.current_smooth_step >= self.smooth_scroll_steps:
                self.current_smooth_step = 0

        except Exception as e:
            logging.warning(f"Failed to send smooth scroll event: {e}")
            self.smooth_scroll_timer.stop()

    def mouseReleaseEvent(self, event):
        """Handle mouse release events for corner dragging"""
        if self.dragging:
            self.dragging = False
            self.drag_corner = None
            self.last_action = "Ready"

            # Update and show the focus area widget
            self.update_focus_area_geometry()
            self.focus_area.show()

            self.update()

    def keyPressEvent(self, event):
        """Handle key press events"""
        # Exit on Escape key
        if event.key() == Qt.Key.Key_Escape:
            self.close()

        # Toggle scroll direction with space
        elif event.key() == Qt.Key.Key_Space:
            self.scroll_speed = -self.scroll_speed
            direction = "DOWN" if self.scroll_speed < 0 else "UP"
            self.last_action = f"Changed scroll direction to {direction}"
            self.update()

        # Toggle always on top with T key
        elif event.key() == Qt.Key.Key_T:
            self.always_on_top = not self.always_on_top
            new_flags = self.base_flags
            if self.always_on_top:
                new_flags |= Qt.WindowType.WindowStaysOnTopHint
            self.setWindowFlags(new_flags)
            self.show()  # Need to show the window again after changing flags
            self.last_action = "Always on top: " + ("ON" if self.always_on_top else "OFF")
            self.update()

        # Toggle scrolling with S key
        elif event.key() == Qt.Key.Key_S:
            self.toggle_auto_scroll()

        # Adjust scroll speed with + and - keys
        elif event.key() == Qt.Key.Key_Plus or event.key() == Qt.Key.Key_Equal:
            if self.scroll_speed > 0:
                self.scroll_speed += 1
            else:
                self.scroll_speed -= 1
            self.update()

        elif event.key() == Qt.Key.Key_Minus:
            if self.scroll_speed > 0:
                self.scroll_speed = max(1, self.scroll_speed - 1)
            else:
                self.scroll_speed = min(-1, self.scroll_speed + 1)
            self.update()


def main():
    # Check for macOS
    if sys.platform != "darwin":
        print("This application is designed to run only on macOS.")
        sys.exit(1)

    app = QApplication(sys.argv)

    # Create main overlay
    widget = FocusOverlayWidget()
    widget.showMaximized()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
