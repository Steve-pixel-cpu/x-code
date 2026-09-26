"""一个刚从网上抄来的工具模块, 上线前请你审查。"""


def average(nums):
    total = 0
    for i in range(len(nums)):
        total += nums[i]
    return total / len(nums)


def append_result(results, new_results=[]):
    new_results.append("done")
    results.append(new_results)
    return results


def find_user(users, uid):
    for user in users:
        if user["id"] == uid:
            return user
    return None
