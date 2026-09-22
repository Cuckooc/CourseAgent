# -*- coding: utf-8 -*-
"""@脚本 osv_scan.py

@作用 安全扫描脚本：本机安装 pip-audit 受限时，直接调用 OSV.dev API 离线排查
      requirements.txt 中已锁定版本依赖的公开漏洞公告（与 pip-audit 使用的
      OSV/PyPI advisory 同源数据，直接调 API 效果等效），解决本地快速安全排查问题。
@运行方式 python scripts/osv_scan.py [requirements 文件路径]
      例：python scripts/osv_scan.py requirements.txt；不传参数时默认读取 requirements.txt。
@主要成员 parse_req()：把单行依赖声明解析为 (包名, 版本)；
      main()：逐包查询 OSV 漏洞库、打印结果并汇总退出码。
@副作用 读取命令行指定的 requirements 文本文件；对 https://api.osv.dev/v1/query 发起
      HTTPS POST 请求（只出不进，不上传项目代码）；仅向标准输出打印扫描结果，
      不写文件、不改环境、无任何删除操作。
@被谁使用 开发/安全排查时人工手动执行，不属于应用运行链路；退出码：发现漏洞=1、
      无漏洞=0；如需在 CI 强制拦截，请在 CI 端安装 pip-audit，本脚本仅用于本地快速排查。
"""
import json
import sys
import urllib.request

# OSV.dev 漏洞批量查询接口（数据源：OSV.dev 官方 API）
API = "https://api.osv.dev/v1/query"


def parse_req(line: str):
    """解析 requirements.txt 中的单行依赖声明。

    被 main() 在读取文件时逐行调用（脚本内部调用）。
    参数 line：requirements 文件的一行原始文本（来源：文件内容），可能含行内注释或 -r/-e 选项行。
    返回：(包名, 版本) 二元组；空行/注释行/“-”开头的选项行返回 None；
          未锁定具体版本时版本号返回 None（调用方会跳过扫描）。
    """
    line = line.split("#")[0].strip()
    if not line or line.startswith("-"):
        return None
    # 按优先级依次尝试常见版本固定符号；命中第一个即按其切分包名与版本
    for sep in ("==", ">=", "~=", "<=", ">", "<", "!="):
        if sep in line:
            name, ver = line.split(sep, 1)
            return name.strip(), ver.strip()
    return (line, None)  # 未锁定版本（如 alembic>=1.13,<1.15 被上面截断）


def main(path: str) -> int:
    """逐包查询 OSV 漏洞库、打印扫描结果并返回进程退出码。

    被脚本入口 if __name__ == "__main__" 块调用（脚本内部调用）。
    参数 path：requirements 文件路径，来源为命令行参数 argv[1]，缺省 "requirements.txt"。
    返回：退出码 int——存在漏洞公告的包返回 1，否则返回 0（去向：sys.exit）。
    异常：单个依赖的网络请求/响应解析异常在循环内就地捕获并打印 ERR，不中断后续扫描。
    """
    findings = []  # 存在漏洞公告的 (包名, 版本, 漏洞列表)
    scanned = 0  # 实际发起查询的锁定版本依赖计数
    for line in open(path, encoding="utf-8"):
        parsed = parse_req(line)
        if not parsed:
            continue
        name, ver = parsed
        if not ver:
            print(f"SKIP (未锁定版本): {name}")
            continue
        scanned += 1
        # 构造 OSV 查询体：声明 PyPI 生态及精确版本
        body = json.dumps({"package": {"name": name, "ecosystem": "PyPI"}, "version": ver}).encode()
        # 请求头声明 JSON 请求体（OSV API 要求）
        req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
        try:
            # timeout=15：单个包查询的超时秒数，避免网络挂起拖死整轮扫描
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            vulns = data.get("vulns", [])
            if vulns:
                findings.append((name, ver, vulns))
                print(f"VULN  {name}=={ver}: {len(vulns)} 条")
                for v in vulns:
                    ids = v.get("id")
                    aliases = ",".join(a for a in v.get("aliases", []) if a.startswith("CVE"))
                    # 摘要截断前 90 个字符，仅用于控制台展示，防止刷屏
                    summary = (v.get("summary") or v.get("details", ""))[:90]
                    fixed = []
                    # 从 affected[].ranges[].events[] 中抽取所有 fixed 事件，即官方给出的修复版本
                    for aff in v.get("affected", []):
                        for r in aff.get("ranges", []):
                            for ev in r.get("events", []):
                                if "fixed" in ev:
                                    fixed.append(ev["fixed"])
                    print(f"      - {ids}{' (' + aliases + ')' if aliases else ''} 修复版本: {','.join(fixed) or '见公告'}")
                    print(f"        {summary}")
            else:
                print(f"OK    {name}=={ver}")
        except Exception as e:
            print(f"ERR   {name}=={ver}: {e}")
    print(f"\n扫描 {scanned} 个锁定版本依赖，发现 {len(findings)} 个存在漏洞公告的包")
    return 1 if findings else 0


if __name__ == "__main__":
    # 执行流程：取命令行第一个参数作为依赖清单路径（未提供则默认 requirements.txt），
    # 调用 main() 完成扫描，并把漏洞判定结果作为进程退出码返回
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"))
