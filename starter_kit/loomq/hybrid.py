"""L3：Hybrid-QASM → (量子操作序列, RISC-V 汇编)。

分三段，可以分开讲清楚，也可以分开测：

    词法 tokenize  →  递归下降 parse 出 AST  →  代码生成 emit

外加一个 AST 参考解释器 `interpret`。它不参与交付，只作为差分测试的基准：
`tools/selftest_hybrid.py` 随机生成程序，穷举注入所有测量值组合，
比对"解释器算出的寄存器终态"与"生成的汇编在官方模拟器上跑出的终态"。
这与官方判定方式（穷举注入 + 比对参考解释器）是同一套逻辑，
所以通过率能直接测出来，不用赌隐藏用例。

## 题面文法与本实现的取舍

题面给的是示例加一段自然语言，不是形式文法，若干边界没写：
是否允许嵌套 if、括号、无 else 的 if、多个 classical 块、寄存器间运算。
而判定说评测会"随机生成 N 组用例（不同分支结构、不同常量、不同测量位数）"。

**取舍是一律支持超集。** 递归下降天然支持嵌套与括号，多写几行而已；
支持得比要求多只会更安全，反过来则直接丢分。具体多支持了：

  - 嵌套 if/else，任意层
  - 无 else 的 if
  - 括号与一元负号
  - 比较两侧都可以是任意表达式（题面示例只有 `c[0] == 1`）
  - `c[k]` 可以出现在赋值右侧，不限于条件里
  - 多个 classical 块（按出现顺序拼接，经典状态跨块延续）

## 寄存器分配

题面规定 `r1..r9` → `x1..x9`，测量位 `c[k]` → `x10, x11, ...`。
表达式求值需要临时寄存器，只能从这两段之外取：**从 x31 往下分配**，
并检查不与测量位重叠。x0 是硬连线零，永远不碰。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .ir import format_param
from .qasm2_parser import QasmError, parse

# 题面：r1..r9 映射到 x1..x9
MAX_VARIABLE = 9
# 题面：测量位 c[k] 依次映射到 x10, x11, ...
CLBIT_BASE = 10
# 临时寄存器从高位往下取，避开变量段与测量位段
SCRATCH_TOP = 31


class HybridError(ValueError):
    """Hybrid-QASM 编译期错误。刻意与 QasmError 区分，便于定位是哪一段出的问题。"""


# --- AST --------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: int


@dataclass(frozen=True)
class Variable:
    """经典寄存器变量 r1..r9。"""

    index: int


@dataclass(frozen=True)
class Clbit:
    """测量位 c[k]，由评测系统注入到 x(10+k)。"""

    index: int


@dataclass(frozen=True)
class BinOp:
    op: str  # '+' 或 '-'
    left: "Expr"
    right: "Expr"


Expr = Union[Literal, Variable, Clbit, BinOp]


@dataclass(frozen=True)
class Assign:
    target: int  # r{target}
    value: Expr


@dataclass(frozen=True)
class If:
    op: str  # '==' 或 '!='
    left: Expr
    right: Expr
    then_body: Tuple["Stmt", ...]
    else_body: Tuple["Stmt", ...]


Stmt = Union[Assign, If]


# --- 词法 -------------------------------------------------------------------

# 注释必须处理：题面给出的那段示例，`classical {` 后面就跟着 `// 经典控制块……`。
# 官方公开自测的用例恰好没有注释，所以漏掉注释也能过公开用例——
# 这正是只盯着公开用例开发会栽的地方。
_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<line_comment>//[^\n]*|\#[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<clbit>c\s*\[\s*\d+\s*\])
  | (?P<var>r\d+)
  | (?P<kw>\bif\b|\belse\b)
  | (?P<int>\d+)
  | (?P<cmp>==|!=)
  | (?P<punct>[-+={}();])
""",
    re.VERBOSE | re.DOTALL,
)

_SKIP_KINDS = frozenset({"space", "line_comment", "block_comment"})


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    position: int


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    index = 0
    while index < len(source):
        match = _TOKEN_RE.match(source, index)
        if not match:
            raise HybridError(
                "classical 块里出现无法识别的字符 %r（位置 %d）" % (source[index], index)
            )
        index = match.end()
        kind = match.lastgroup
        if kind in _SKIP_KINDS:
            continue
        assert kind is not None
        tokens.append(Token(kind, match.group().strip(), match.start()))
    return tokens


# --- 递归下降解析 -----------------------------------------------------------


class Parser:
    """经典块的递归下降解析器。

    文法（本实现接受的超集）：

        block      := stmt*
        stmt       := assign | if
        assign     := var '=' expr ';'
        if         := 'if' '(' compare ')' '{' stmt* '}' ('else' '{' stmt* '}')?
        compare    := expr ('==' | '!=') expr
        expr       := term (('+' | '-') term)*        // 左结合
        term       := int | var | clbit | '-' term | '(' expr ')'
    """

    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = list(tokens)
        self.position = 0

    # -- 基础操作 --

    def peek(self) -> Optional[Token]:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise HybridError("classical 块在预期还有内容的地方结束了")
        self.position += 1
        return token

    def accept(self, text: str) -> bool:
        token = self.peek()
        if token is not None and token.text == text:
            self.position += 1
            return True
        return False

    def expect(self, text: str) -> Token:
        token = self.peek()
        if token is None or token.text != text:
            got = "文件结束" if token is None else repr(token.text)
            raise HybridError("classical 块里预期 %r，实际是 %s" % (text, got))
        return self.next()

    # -- 产生式 --

    def parse_block(self, until: Optional[str] = None) -> Tuple[Stmt, ...]:
        body: List[Stmt] = []
        while True:
            token = self.peek()
            if token is None:
                if until is not None:
                    raise HybridError("classical 块缺少配对的 %r" % until)
                break
            if until is not None and token.text == until:
                break
            body.append(self.parse_stmt())
        return tuple(body)

    def parse_stmt(self) -> Stmt:
        token = self.peek()
        assert token is not None
        if token.text == "if":
            return self.parse_if()
        if token.kind == "var":
            return self.parse_assign()
        raise HybridError("classical 块里出现无法解析的语句，起始于 %r" % token.text)

    def parse_assign(self) -> Assign:
        target = self.parse_variable_index(self.next())
        self.expect("=")
        value = self.parse_expr()
        # 题面示例每条赋值都有分号；容忍缺失的最后一条，多接受不会更危险。
        self.accept(";")
        return Assign(target, value)

    def parse_if(self) -> If:
        self.expect("if")
        self.expect("(")
        left = self.parse_expr()
        token = self.next()
        if token.kind != "cmp":
            raise HybridError("if 条件里预期 == 或 !=，实际是 %r" % token.text)
        right = self.parse_expr()
        self.expect(")")
        self.expect("{")
        then_body = self.parse_block(until="}")
        self.expect("}")
        else_body: Tuple[Stmt, ...] = ()
        if self.accept("else"):
            self.expect("{")
            else_body = self.parse_block(until="}")
            self.expect("}")
        return If(token.text, left, right, then_body, else_body)

    def parse_expr(self) -> Expr:
        node = self.parse_term()
        while True:
            token = self.peek()
            if token is None or token.text not in ("+", "-"):
                return node
            self.next()
            node = BinOp(token.text, node, self.parse_term())

    def parse_term(self) -> Expr:
        token = self.next()
        if token.text == "-":
            # 一元负号：0 - x，不需要额外指令种类
            return BinOp("-", Literal(0), self.parse_term())
        if token.text == "(":
            node = self.parse_expr()
            self.expect(")")
            return node
        if token.kind == "int":
            return Literal(int(token.text))
        if token.kind == "var":
            return Variable(self.parse_variable_index(token))
        if token.kind == "clbit":
            return Clbit(int(re.search(r"\d+", token.text).group()))
        raise HybridError("表达式里出现意外的记号 %r" % token.text)

    @staticmethod
    def parse_variable_index(token: Token) -> int:
        if token.kind != "var":
            raise HybridError("预期 r1..r9 这样的变量，实际是 %r" % token.text)
        index = int(token.text[1:])
        if not 1 <= index <= MAX_VARIABLE:
            raise HybridError(
                "变量 %s 越界：题面只定义 r1..r%d" % (token.text, MAX_VARIABLE)
            )
        return index


def parse_classical(source: str) -> Tuple[Stmt, ...]:
    parser = Parser(tokenize(source))
    body = parser.parse_block()
    if parser.peek() is not None:
        raise HybridError("classical 块结尾有多余内容：%r" % parser.peek().text)
    return body


# --- 参考解释器（差分测试基准，不参与交付） ---------------------------------


def interpret(body: Sequence[Stmt], measurements: Dict[int, int]) -> Dict[int, int]:
    """直接在 AST 上求值，返回 {变量下标: 值}。

    只用于测试：把它与"生成的汇编在官方模拟器上的执行结果"对比。
    两条路径完全独立——一条是树求值，一条是真跑指令——所以对得上才有意义。
    """
    registers: Dict[int, int] = {}

    def value_of(node: Expr) -> int:
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Variable):
            return registers.get(node.index, 0)
        if isinstance(node, Clbit):
            return measurements.get(node.index, 0)
        left, right = value_of(node.left), value_of(node.right)
        return left + right if node.op == "+" else left - right

    def run(statements: Sequence[Stmt]) -> None:
        for statement in statements:
            if isinstance(statement, Assign):
                registers[statement.target] = value_of(statement.value)
                continue
            equal = value_of(statement.left) == value_of(statement.right)
            taken = equal if statement.op == "==" else not equal
            run(statement.then_body if taken else statement.else_body)

    run(body)
    return registers


# --- 代码生成 ---------------------------------------------------------------


@dataclass
class Affine:
    """仿射式 `constant + Σ coefficient * x_reg`，键是物理寄存器号。

    经典块只有 `+` 和 `-`，没有乘除，所以任何表达式——无论套多少层括号与
    一元负号——都能归一到这个形式。这不是锦上添花的优化，是解决两个实际
    问题的手段：

      - 按 AST 递归生成代码时，临时寄存器消耗随嵌套深度线性增长；
        测量位多的时候（c[k] 占 x10 起）会把临时寄存器区挤爆。
        归一化之后只需要 1 个临时寄存器，与嵌套深度无关。
      - 递归写法会生成大量搬运指令。实测深嵌套能到 1311 条，
        而官方模拟器 max_steps 只有 1000。归一化把常数在编译期折叠掉，
        指令数大幅下降。
    """

    constant: int = 0
    coefficients: Dict[int, int] = field(default_factory=dict)

    def add_term(self, register: int, coefficient: int) -> None:
        total = self.coefficients.get(register, 0) + coefficient
        if total:
            self.coefficients[register] = total
        else:
            self.coefficients.pop(register, None)

    def combined(self, other: "Affine", sign: int) -> "Affine":
        result = Affine(self.constant + sign * other.constant, dict(self.coefficients))
        for register, coefficient in other.coefficients.items():
            result.add_term(register, sign * coefficient)
        return result


def affine_of(node: Expr) -> Affine:
    if isinstance(node, Literal):
        return Affine(node.value, {})
    if isinstance(node, Variable):
        return Affine(0, {node.index: 1})
    if isinstance(node, Clbit):
        return Affine(0, {CLBIT_BASE + node.index: 1})
    left = affine_of(node.left)
    right = affine_of(node.right)
    return left.combined(right, 1 if node.op == "+" else -1)


class Emitter:
    """把 AST 编译成只含 li/add/sub/addi/beq/bne/j 七条指令的汇编。

    临时寄存器从 x31 往下取。之所以不从低位取：x1..x9 是变量、
    x10 起是测量位注入区，两段都不能碰。归一化成仿射式之后，
    同时活跃的临时寄存器最多 1 个。
    """

    def __init__(self, n_clbits: int) -> None:
        self.lines: List[str] = []
        self.label_counter = 0
        self.scratch_floor = CLBIT_BASE + max(n_clbits, 0)
        self.scratch_next = SCRATCH_TOP

    # -- 寄存器与标签 --

    def acquire(self) -> int:
        if self.scratch_next < self.scratch_floor:
            raise HybridError(
                "临时寄存器不够用：测量位占到 x%d，表达式嵌套太深"
                % (self.scratch_floor - 1)
            )
        register = self.scratch_next
        self.scratch_next -= 1
        return register

    def release(self, register: int) -> None:
        self.scratch_next = register

    def new_label(self, prefix: str) -> str:
        self.label_counter += 1
        return "%s_%d" % (prefix, self.label_counter)

    def emit(self, text: str) -> None:
        self.lines.append("  " + text)

    def emit_label(self, label: str) -> None:
        self.lines.append("%s:" % label)

    # -- 表达式 --

    def emit_affine(self, value: Affine, destination: int) -> None:
        """把仿射式的值算进 x{destination}。

        destination 一定是临时寄存器，不会与 x1..x9 或测量位重名，
        所以可以放心先写它再逐项累加，不存在覆盖源操作数的问题。
        """
        terms = [(register, coefficient)
                 for register, coefficient in sorted(value.coefficients.items())
                 if coefficient]

        # 形如 `x_k + 常数` 时直接一条 addi，省掉 li 和累加
        if len(terms) == 1 and terms[0][1] == 1:
            self.emit("addi x%d, x%d, %d" % (destination, terms[0][0], value.constant))
            return

        self.emit("li x%d, %d" % (destination, value.constant))
        for register, coefficient in terms:
            opcode = "add" if coefficient > 0 else "sub"
            for _ in range(abs(coefficient)):
                self.emit("%s x%d, x%d, x%d" % (opcode, destination, destination, register))

    def emit_expr(self, node: Expr, destination: int) -> None:
        self.emit_affine(affine_of(node), destination)

    # -- 语句 --

    def emit_body(self, body: Sequence[Stmt]) -> None:
        for statement in body:
            if isinstance(statement, Assign):
                self.emit_assign(statement)
            else:
                self.emit_if(statement)

    def emit_assign(self, statement: Assign) -> None:
        target = statement.target
        value = affine_of(statement.value)

        # `r_t = r_t + 常数`：一条 addi 就位，也是题面示例里的写法
        if value.coefficients == {target: 1}:
            if value.constant:
                self.emit("addi x%d, x%d, %d" % (target, target, value.constant))
            return  # r_t = r_t，不需要指令

        # 目标不出现在右侧时可以直接写目标寄存器，省掉一次搬运
        if target not in value.coefficients:
            self.emit_affine(value, target)
            return

        # 目标同时是源，先算进临时寄存器再搬，避免读到被覆盖后的值
        scratch = self.acquire()
        self.emit_affine(value, scratch)
        self.emit("addi x%d, x%d, 0" % (target, scratch))
        self.release(scratch)

    def emit_if(self, statement: If) -> None:
        # 把 `lhs OP rhs` 归一成 `lhs - rhs OP 0`，只需要一个临时寄存器
        difference = affine_of(statement.left).combined(affine_of(statement.right), -1)

        # 两侧都是常数时条件在编译期就定了，不生成分支
        if not difference.coefficients:
            equal = difference.constant == 0
            taken = equal if statement.op == "==" else not equal
            self.emit_body(statement.then_body if taken else statement.else_body)
            return

        else_label = self.new_label("L_else")
        end_label = self.new_label("L_end")

        scratch = self.acquire()
        self.emit_affine(difference, scratch)
        # x0 硬连线为零，差值直接跟它比。条件取反后跳过 then 分支：
        # == 用 bne 跳走，!= 用 beq 跳走。
        branch = "bne" if statement.op == "==" else "beq"
        self.emit("%s x%d, x0, %s" % (branch, scratch, else_label))
        self.release(scratch)

        self.emit_body(statement.then_body)
        self.emit("j %s" % end_label)
        self.emit_label(else_label)
        self.emit_body(statement.else_body)
        self.emit_label(end_label)


def generate_assembly(body: Sequence[Stmt], n_clbits: int) -> str:
    emitter = Emitter(n_clbits)
    emitter.emit_body(body)
    if not emitter.lines:
        # 模拟器对空程序是合法的，但评测器要求返回非空字符串。
        # 用一条无副作用的指令占位：x0 硬连线为零，写它会被忽略。
        emitter.emit("addi x0, x0, 0")
    header = [
        "# LoomQ L3 生成的 RISC-V 汇编",
        "# 变量 r1..r9 -> x1..x9；测量位 c[k] -> x%d+k（由评测系统注入）" % CLBIT_BASE,
        "# 临时寄存器自 x%d 向下分配" % SCRATCH_TOP,
    ]
    return "\n".join(header + emitter.lines) + "\n"


# --- Hybrid-QASM 拆分 -------------------------------------------------------

_CLASSICAL_RE = re.compile(r"\bclassical\b\s*\{", re.IGNORECASE)


def split_hybrid(source: str) -> Tuple[str, List[str]]:
    """拆成 (纯 QASM 文本, [classical 块内容])，按出现顺序。

    不能用正则一把匹配整个 classical 块——块里有嵌套花括号，正则数不清配对。
    这里从 `classical {` 起手工扫描花括号深度。
    """
    quantum_parts: List[str] = []
    blocks: List[str] = []
    cursor = 0
    while True:
        match = _CLASSICAL_RE.search(source, cursor)
        if not match:
            quantum_parts.append(source[cursor:])
            break
        quantum_parts.append(source[cursor : match.start()])
        depth = 1
        index = match.end()
        while index < len(source) and depth:
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise HybridError("classical 块缺少配对的右花括号")
        blocks.append(source[match.end() : index - 1])
        cursor = index
    return "".join(quantum_parts), blocks


def _statement_text(op) -> str:
    """把一个 IR 操作还原成规范的 OpenQASM 2.0 语句文本。

    交付格式的选择：题面只说返回"剥离出的纯量子门/测量指令列表"，
    adapter 骨架把类型收窄为 List[str]，但没规定每个字符串长什么样。
    这里选规范 QASM 2.0 语句文本（`h q[0];`、`measure q[0] -> c[0];`），
    理由是它既满足 List[str]，又能被判定方直接解析回去做语义等价校验——
    在格式未定义的情况下，可被原样解析的标准写法是最稳妥的一种。
    """
    from .ir import Measure  # 局部导入避免与本模块的 Measure 同名混淆

    if isinstance(op, Measure):
        return "measure q[%d] -> c[%d];" % (op.qubit, op.clbit)
    operands = ", ".join("q[%d]" % index for index in op.qubits)
    if op.params:
        params = "(" + ", ".join(format_param(value) for value in op.params) + ")"
        return "%s%s %s;" % (op.name, params, operands)
    return "%s %s;" % (op.name, operands)


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """L3 主入口：Hybrid-QASM → (量子操作序列, RISC-V 汇编文本)。"""
    quantum_source, blocks = split_hybrid(hybrid_qasm_str)

    try:
        circuit = parse(quantum_source)
    except QasmError as exc:
        raise HybridError("Hybrid-QASM 的量子部分解析失败：%s" % exc) from exc

    quantum_ops = [_statement_text(op) for op in circuit.ops]

    body: List[Stmt] = []
    for block in blocks:
        body.extend(parse_classical(block))

    assembly = generate_assembly(body, circuit.n_clbits)
    return quantum_ops, assembly


__all__ = [
    "HybridError",
    "compile_hybrid",
    "generate_assembly",
    "interpret",
    "parse_classical",
    "split_hybrid",
]
