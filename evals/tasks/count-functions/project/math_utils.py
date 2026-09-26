def clamp(value, low, high):
    return max(low, min(high, value))


def mean(nums):
    if not nums:
        raise ValueError("empty")
    return sum(nums) / len(nums)


class Accumulator:
    """注意: 类方法不算函数。"""

    def __init__(self):
        self.total = 0

    def add(self, n):
        self.total += n
