# 技能系统 — 可扩展的知识注入

> DeerFlow Agent Harness 深度分析 · 第 7 篇

---

## 1. 概述与定位

技能（Skills）是 DeerFlow 的知识注入机制——将领域知识、操作指南、最佳实践以 Markdown 文档形式注入 Agent 的上下文。不同于工具（Tool）提供"能力"，技能提供"知识"。

```
┌──────────────────────────────────────────────────────────────┐
│                    Skills Pipeline                             │
│                                                                │
│  skills/{public,custom}/                                       │
│    │                                                           │
│    ├── 发现: _iter_skill_files() → 递归扫描 SKILL.md           │
│    ├── 解析: parse_skill_file() → YAML frontmatter + 内容      │
│    ├── 验证: _validate_skill_frontmatter() → 字段检查          │
│    ├── 安全: scan_skill_security() → 危险模式检测              │
│    ├── 启用: ExtensionsConfig → enabled 状态合并               │
│    │                                                           │
│    └── 注入: apply_prompt_template() → 系统提示词              │
│          "Available skills: /mnt/skills/public/my-skill/"     │
└──────────────────────────────────────────────────────────────┘
```

### 解决的核心问题

1. **知识注入**：将领域知识注入 Agent 上下文，无需修改代码
2. **渐进加载**：仅在任务需要时加载技能，保持上下文窗口精简
3. **安全门控**：防止恶意技能执行危险代码
4. **可扩展性**：社区技能 + 自定义技能 + 安装技能
5. **版本管理**：自定义技能的编辑历史和回滚

### 一句话设计哲学

**"技能是 Agent 的知识包——声明式定义、安全门控加载、渐进式注入。"**

---

## 2. 架构总览

### 2.1 技能格式：SKILL.md

```markdown
---
name: my-skill
description: Description of what this skill does
license: MIT
---

# My Skill

Detailed instructions for the agent...
```

YAML frontmatter 定义元数据，Markdown 正文定义知识内容。

### 2.2 组件架构

| 组件 | 文件 | 职责 |
|------|------|------|
| **Skill** | `types.py` | 技能数据类 |
| **parse_skill_file** | `parser.py` | YAML frontmatter 解析 |
| **_validate_skill_frontmatter** | `validation.py` | 字段验证 |
| **scan_skill_security** | `security_scanner.py` | 危险模式扫描 |
| **SkillStorage** | `storage/skill_storage.py` | 存储抽象 + 模板方法 |
| **LocalSkillStorage** | `storage/local_skill_storage.py` | 文件系统实现 |
| **install_skill_from_archive** | `installer.py` | .skill ZIP 安装 |

---

## 3. 源码走读

### 3.1 Skill 数据类

```python
@dataclass
class Skill:
    name: str                              # 技能名称
    description: str                       # 描述
    license: str | None                    # 许可证
    skill_dir: Path                        # 技能目录路径
    skill_file: Path                       # SKILL.md 文件路径
    relative_path: Path                    # 相对于分类根的路径
    category: SkillCategory                # PUBLIC 或 CUSTOM
    enabled: bool = False                  # 是否启用

    @property
    def skill_path(self) -> str:
        """POSIX 路径字符串。"""
        return self.relative_path.as_posix() if self.relative_path != Path(".") else ""

    def get_container_path(self, container_base_path="/mnt/skills") -> str:
        """虚拟路径：/mnt/skills/{category}/{skill_path}"""
        return f"{container_base_path}/{self.category.value}/{self.skill_path}"

    def get_container_file_path(self, container_base_path="/mnt/skills") -> str:
        """SKILL.md 虚拟路径"""
        return f"{self.get_container_path(container_base_path)}/SKILL.md"
```

### 3.2 技能解析：`parse_skill_file()`

```python
def parse_skill_file(skill_file: Path, category: SkillCategory,
                     relative_path: Path | None = None) -> Skill | None:
    """解析 SKILL.md 文件。"""
    if not skill_file.exists() or skill_file.name != SKILL_MD_FILE:
        return None

    content = skill_file.read_text(encoding="utf-8")

    # 提取 YAML frontmatter
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return None

    frontmatter = yaml.safe_load(match.group(1))

    # 验证必填字段
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name or not isinstance(name, str) or not name.strip():
        return None
    if not description or not isinstance(description, str) or not description.strip():
        return None

    license = str(frontmatter.get("license", "")).strip() or None

    return Skill(
        name=name.strip(),
        description=description.strip(),
        license=license,
        skill_dir=skill_file.parent,
        skill_file=skill_file,
        relative_path=relative_path or Path("."),
        category=category,
        enabled=True,  # 实际状态由 ExtensionsConfig 决定
    )
```

### 3.3 安全扫描：`scan_skill_security()`

```python
_DANGEROUS_PATTERNS = [
    re.compile(r"\beval\s*\("),           # eval() 动态执行
    re.compile(r"\bexec\s*\("),           # exec() 动态执行
    re.compile(r"__import__"),            # 动态导入
    re.compile(r"\bsubprocess\b"),        # 子进程
    re.compile(r"\bos\.system\b"),        # 系统调用
    re.compile(r"\bos\.popen\b"),         # 管道执行
    re.compile(r"\bopen\s*\([^)]*['\"]w"), # 写文件
    re.compile(r"\bshutil\.rmtree\b"),    # 递归删除
    re.compile(r"\bsocket\b"),            # 网络连接
    re.compile(r"\brequests\b"),          # HTTP 请求
    re.compile(r"\burllib\b"),            # URL 操作
    re.compile(r"\bpickle\b"),            # 反序列化（RCE 风险）
    re.compile(r"\bmarshal\b"),           # 字节码序列化
    re.compile(r"\bctypes\b"),            # FFI（系统调用）
    re.compile(r"\bcompile\s*\("),        # 动态编译
]

def scan_skill_security(skill_dir: Path, skill_name: str) -> list[str]:
    """扫描技能目录中的所有文件，检测危险模式。"""
    warnings = []
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # 跳过隐藏目录
        for filename in files:
            file_path = Path(root) / filename
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for line_num, line in enumerate(content.splitlines(), 1):
                    for pattern in _DANGEROUS_PATTERNS:
                        if pattern.search(line):
                            warnings.append(
                                f"[{skill_name}] {file_path.name}:{line_num}: "
                                f"potentially dangerous pattern: {line.strip()[:100]}"
                            )
            except Exception:
                pass
    return warnings
```

**设计洞察**：安全扫描不是阻止所有危险代码——技能可能需要 `requests`（如 API 调用技能）。扫描结果作为**警告**，安装时决定是否继续。

### 3.4 技能安装：`ainstall_skill_from_archive()`

```python
async def ainstall_skill_from_archive(self, archive_path: str | Path) -> dict:
    """从 .skill ZIP 包安装技能。"""
    # 1. 验证文件扩展名
    archive_path = Path(archive_path)
    if archive_path.suffix != ".skill":
        raise ValueError(f"Archive must have .skill extension, got {archive_path.suffix}")

    # 2. 安全解压到临时目录
    with tempfile.TemporaryDirectory() as tmp_dir:
        extract_dir = Path(tmp_dir)
        with zipfile.ZipFile(archive_path) as zf:
            safe_extract_skill_archive(zf, extract_dir)  # 路径遍历防护

        # 3. 定位 SKILL.md
        skill_dir = resolve_skill_dir_from_archive(extract_dir)

        # 4. 验证 frontmatter
        is_valid, message, skill_name = _validate_skill_frontmatter(skill_dir)
        if not is_valid:
            raise ValueError(f"Invalid skill: {message}")

        # 5. 检查重复
        if self.custom_skill_exists(skill_name):
            raise SkillAlreadyExistsError(f"Skill '{skill_name}' already exists")

        # 6. 安全扫描
        security_warnings = scan_skill_security(skill_dir, skill_name)
        if security_warnings:
            raise SkillSecurityScanError(
                f"Security scan found {len(security_warnings)} warning(s):\n"
                + "\n".join(security_warnings[:10])
            )

        # 7. 原子安装：stage → rename
        final_target = self.get_custom_skill_dir(skill_name)
        staging_target = final_target.with_suffix(".staging")
        shutil.copytree(skill_dir, staging_target)
        _move_staged_skill_into_reserved_target(staging_target, final_target)

    return {"success": True, "skill_name": skill_name, "message": "Skill installed successfully"}
```

**路径遍历防护**：

```python
def safe_extract_skill_archive(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """安全解压：防止 ZIP 路径遍历攻击。"""
    for member in zf.namelist():
        member_path = target_dir / member
        # 检查解析后的路径是否在目标目录内
        if not member_path.resolve().startswith(target_dir.resolve()):
            raise ValueError(f"Path traversal attempt: {member}")
        if member.startswith("/") or ".." in member:
            raise ValueError(f"Dangerous path in archive: {member}")
        zf.extract(member, target_dir)
```

### 3.5 技能加载：`load_skills()`

```python
def load_skills(self, *, enabled_only: bool = False) -> list[Skill]:
    """加载所有技能，合并启用状态。"""
    skills = []
    for category, category_root, skill_md_path in self._iter_skill_files():
        relative = skill_md_path.parent.relative_to(category_root)
        skill = parse_skill_file(skill_md_path, category, relative)
        if skill is not None:
            skills.append(skill)

    # 合并启用状态（从 ExtensionsConfig）
    extensions_config = ExtensionsConfig.from_file()  # 每次重读
    enabled_skills = extensions_config.get_enabled_skills()
    for skill in skills:
        if skill.name in enabled_skills:
            skill.enabled = True

    if enabled_only:
        skills = [s for s in skills if s.enabled]

    return sorted(skills, key=lambda s: s.name)
```

### 3.6 自定义技能编辑/回滚

```python
def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
    """原子写入自定义技能。"""
    # 验证名称和路径
    validate_skill_name(name)
    target = self.get_custom_skill_file(name)
    if relative_path:
        target = self.ensure_safe_support_path(name, relative_path)

    # 原子写入：temp + replace
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", dir=target.parent, delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)  # POSIX atomic rename

def append_history(self, name: str, record: dict) -> None:
    """追加编辑历史。"""
    history_file = self.get_skill_history_file(name)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    record["timestamp"] = datetime.now(UTC).isoformat()
    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def read_history(self, name: str) -> list[dict]:
    """读取编辑历史。"""
    history_file = self.get_skill_history_file(name)
    if not history_file.exists():
        return []
    records = []
    for line in history_file.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records
```

---

## 4. 核心机制详解

### 4.1 安全门控加载

技能加载经过三道门：

```
发现 → 解析 → 验证 → 安全扫描 → 启用状态合并 → 注入
         │       │         │
         │       │         └── 危险模式检测（安装时阻止）
         │       └── 必填字段 + 未知属性警告
         └── YAML frontmatter 提取
```

### 4.2 渐进加载

技能不直接注入系统提示词的全文，而是注入**容器路径**：

```
系统提示词中：
"Available skills:
  - /mnt/skills/public/deep-research/  - Deep research methodology
  - /mnt/skills/custom/my-workflow/    - My custom workflow"
```

Agent 需要时调用 `read_file("/mnt/skills/public/deep-research/SKILL.md")` 读取完整内容。这实现了**按需加载**——未使用的技能不占上下文。

### 4.3 原子写入

所有文件写入使用 `tempfile + Path.replace()` 模式：

```python
# 1. 写入临时文件
with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", dir=target.parent, delete=False) as tmp:
    tmp.write(content)
    tmp_path = Path(tmp.name)

# 2. 原子重命名（POSIX 保证原子性）
tmp_path.replace(target)
```

如果写入过程中崩溃，临时文件残留但目标文件不变，不会出现半写状态。

---

## 5. 设计模式提取

### 5.1 模板方法模式（Template Method）

`SkillStorage.load_skills()` 是模板方法——定义加载流程骨架（迭代 → 解析 → 合并 → 过滤 → 排序），子类通过 `_iter_skill_files()` 提供具体的文件迭代逻辑。

### 5.2 策略模式（Strategy）

`SkillStorage` 是策略接口，`LocalSkillStorage` 是文件系统策略。未来可添加 `GitSkillStorage`、`S3SkillStorage` 等。

### 5.3 安全门控模式

三阶段安全检查（验证 → 扫描 → 路径遍历防护）是"安全门控"模式的实现。

---

## 6. 业界对比

| 特性 | DeerFlow Skills | Claude Code Skills | GPTs Custom Instructions | Cursor Rules |
|------|----------------|-------------------|-------------------------|-------------|
| **格式** | SKILL.md (YAML + Markdown) | CLAUDE.md (Markdown) | 纯文本 | .cursorrules |
| **安全扫描** | 14 种危险模式 | 无 | 无 | 无 |
| **安装** | .skill ZIP 包 | 文件复制 | UI 输入 | 文件复制 |
| **编辑历史** | JSONL 日志 | 无 | 无 | 无 |
| **原子写入** | temp + replace | 无 | 无 | 无 |
| **延迟加载** | 容器路径 + 按需读取 | 全量注入 | 全量注入 | 全量注入 |
| **分类** | public/custom | 单一 | 单一 | 单一 |

**DeerFlow 的独特之处**：
1. **安全扫描**：其他系统信任用户输入，DeerFlow 扫描危险模式
2. **延迟加载**：只注入路径，按需读取全文
3. **原子写入**：防止半写状态
4. **编辑历史**：支持回滚

---

## 7. 面试关联

### Q1: Agent 技能/知识注入有哪些模式？

**加分项**：

> "三种模式：**全量注入**（如 GPTs Custom Instructions，所有知识始终在上下文中）、**按需加载**（如 DeerFlow Skills，只注入路径，Agent 需要时读取全文）、**检索增强**（如 RAG，按相关性检索知识片段）。DeerFlow 选择按需加载的权衡是：全量注入简单但浪费上下文，检索增强灵活但需要向量数据库，按需加载是中间方案——Agent 自主决定何时加载，无需额外基础设施。"

### Q2: 安全扫描的必要性？

**加分项**：

> "在多用户/多技能市场中，安全扫描是必要的。DeerFlow 扫描 14 种危险模式（eval/exec/subprocess/pickle/ctypes 等），这些模式可能导致远程代码执行（RCE）。关键细节是**路径遍历防护**——ZIP 解压时验证每个成员的解析路径在目标目录内，防止 `../../etc/passwd` 类攻击。扫描不是阻止所有危险代码（技能可能需要 `requests`），而是让安装者知情决策。"

---

## 8. 扩展思考

### 8.1 局限与改进方向

| 局限 | 改进方向 |
|------|---------|
| 安全扫描仅正则，易误报 | AST 级别分析（更精确） |
| 无技能依赖声明 | 支持 `depends: [other-skill]` |
| 无技能版本 | 支持 `version: 1.2.0` + 语义版本约束 |
| 延迟加载依赖 Agent 主动读取 | 自动检测任务相关性 + 预加载 |
| 无技能市场 | 构建类似 npm 的技能注册中心 |

### 8.2 与前沿研究/产品的关联

- **Claude Code CLAUDE.md**：类似 SKILL.md，但无安全扫描和延迟加载
- **GPTs Custom Instructions**：更简单但更直接，适合非技术用户
- **Cursor Rules**：代码级规则，DeerFlow 的技能更偏向知识注入
- **DSPy Modules**：程序化的技能定义，DeerFlow 的声明式更简单
