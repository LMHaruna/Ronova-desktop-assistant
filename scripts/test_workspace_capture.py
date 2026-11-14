"""Offline geometry and image-layer regression checks for workspace screenshots."""
import unittest

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from client_capture import ScreenFrame, crop_selection, selection_rect
from client_markup import Mark, MarkupCanvas, MarkupEditor, marked_image
from client_workspace import Picture


def solid(width, height, color):
    result = QImage(width,height,QImage.Format.Format_RGB32)
    result.fill(QColor(color))
    return result


class WorkspaceCaptureTests(unittest.TestCase):
    def test_crop_uses_native_pixels_across_mixed_scale_negative_origin(self):
        left = ScreenFrame(QRect(-180,0,120,60),solid(180,90,'#2222FF'))
        right = ScreenFrame(QRect(0,0,120,90),solid(120,90,'#FF4400'))
        region = selection_rect(QPoint(23,41),QPoint(-17,11))
        output = crop_selection([left,right],region)
        self.assertEqual((output.width(),output.height()),(40,30))
        self.assertEqual(output.pixelColor(16,10).name(),'#2222ff')
        self.assertEqual(output.pixelColor(17,10).name(),'#ff4400')

    def test_capture_ignores_empty_border_outside_all_monitors(self):
        screen = ScreenFrame(QRect(0,0,80,60),solid(80,60,'#F2EEED'))
        output = crop_selection([screen],QRect(-12,-10,42,35))
        self.assertEqual((output.width(),output.height()),(30,25))
        self.assertTrue(crop_selection([screen],QRect(100,100,9,9)).isNull())

    def test_sent_png_and_preview_share_marks_without_editing_original(self):
        original = solid(240,140,'#194073')
        marks = (Mark('line',((36,70),(180,70))),Mark('rect',((60,32),(150,111))))
        preview = marked_image(original,marks)
        picture = Picture(original,marks)
        self.assertEqual(original.pixelColor(105,70).name(),'#194073')
        self.assertEqual(preview.pixelColor(105,70).name(),'#8b1015')
        self.assertEqual(QImage.fromData(picture.png,'PNG').convertToFormat(preview.format()),preview)
        self.assertIs(picture.png,picture.png)

    def test_eraser_drag_cuts_pen_stroke_and_undo_restores_it_in_one_step(self):
        app = QApplication.instance() or QApplication([])
        original = solid(240,140,'#194073')
        mark = Mark('pen', ((20,70),(220,70)))
        canvas = MarkupCanvas()
        self.addCleanup(canvas.close)
        canvas.load(original, (mark,))
        canvas.scale_to(1)
        canvas.tool = 'erase'
        canvas.show()
        app.processEvents()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120,50))
        QTest.mouseMove(canvas, QPoint(120,90))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120,90))
        self.assertEqual(len(canvas.marks), 2)
        erased = marked_image(original, canvas.marks)
        self.assertEqual(erased.pixelColor(120,70), original.pixelColor(120,70))
        self.assertNotEqual(erased.pixelColor(70,70), original.pixelColor(70,70))
        canvas.undo()
        self.assertEqual(canvas.marks, (mark,))
        canvas.redo()
        self.assertEqual(len(canvas.marks), 2)

    def test_markup_completion_and_cancel_are_adjacent_on_the_bottom_bar(self):
        app = QApplication.instance() or QApplication([])
        editor = MarkupEditor()
        self.addCleanup(editor.close)
        editor.resize(640, 460)
        editor.show()
        marks = (Mark('line', ((10, 10), (50, 50))),)
        editor.open(solid(240, 140, '#194073'), marks)
        app.processEvents()
        done = editor.findChild(QPushButton, 'markupDone')
        buttons = editor.findChildren(QPushButton, options=Qt.FindChildOption.FindDirectChildrenOnly)
        self.assertEqual(buttons, [])
        self.assertIs(done.parentWidget(), editor.bar)
        tools = editor.bar.layout()
        cancel = tools.itemAt(tools.indexOf(done) - 1).widget()
        self.assertEqual(cancel.accessibleName(), '关闭标记并放弃修改')
        self.assertFalse(done.icon().isNull())
        saved, discarded = [], []
        editor.accepted.connect(saved.append)
        editor.cancelled.connect(lambda: discarded.append(True))
        QTest.mouseClick(done, Qt.MouseButton.LeftButton)
        QTest.mouseClick(cancel, Qt.MouseButton.LeftButton)
        self.assertEqual(saved, [marks])
        self.assertEqual(discarded, [True])
