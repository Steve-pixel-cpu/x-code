def parse_duration(text):
    """把时长字符串解析成秒数 (int)。

    规格 (实现须严格遵循):
    - 由数字 + 单位段连续拼接而成, 如 "1h30m", "45s", "2d4h30m"
    - 支持的单位: s(秒) m(分) h(时) d(天)
    - 顺序必须从大到小 (d -> h -> m -> s), 每种单位至多出现一次
    - 返回各段之和的秒数, 如 "1h30m" -> 5400
    - 空串、空段 (如 "hm")、未知单位、顺序不对、单位重复 -> raise ValueError
    """
    raise NotImplementedError
