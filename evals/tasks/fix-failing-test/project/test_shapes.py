from shapes import area_rectangle, perimeter_rectangle


def test_area():
    assert area_rectangle(3, 4) == 12


def test_area_zero():
    assert area_rectangle(0, 5) == 0


def test_perimeter():
    assert perimeter_rectangle(3, 4) == 14
