独立核对任务、代码变更和真实测试记录。

任务：
{task}

执行证据：
{upstream}

检查需求是否实现、测试是否真实通过，是否擅自改动测试或用副作用绕过要求。
只返回一个 JSON 对象，包含 verdict（pass/fail）和 issues 数组。
pass 时 issues 必须为空。fail 时每项必须包含工作区内相对路径 file、具体 problem 和可执行 expected。
没有可验证问题时不要凑数；不得输出思考过程或替换代码。
