import os
import sys
import json
import requests
from typing import List, Dict, Optional

# 配置文件默认保存在脚本同级目录下
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cf_config.json")


class CloudflareDNSManager:
    def __init__(self, api_token: str):
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        })

    def get_zone_id(self, domain: str) -> Optional[str]:
        """根据二级域名（如 example.com）获取对应的 Zone ID"""
        url = f"{self.base_url}/zones"
        params = {"name": domain, "status": "active"}
        try:
            resp = self.session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") and data.get("result"):
                return data["result"][0]["id"]
            print(f"[!] 未找到域名 {domain} 的 Zone ID，请检查域名拼写或 Token 权限。")
            return None
        except requests.RequestException as e:
            print(f"[!] 请求 Zone ID 失败 ({domain}): {e}")
            return None

    def list_ip_records(self, zone_id: str, record_types: tuple = ("A", "AAAA")) -> List[Dict]:
        """查询指定 Zone 下的所有 IP 解析记录（默认包含 IPv4 A 记录和 IPv6 AAAA 记录）"""
        records = []
        url = f"{self.base_url}/zones/{zone_id}/dns_records"

        for r_type in record_types:
            page = 1
            while True:
                params = {
                    "type": r_type,
                    "per_page": 100,
                    "page": page
                }
                try:
                    resp = self.session.get(url, params=params, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                    if not data.get("success"):
                        print(f"[!] 查询记录失败: {data.get('errors')}")
                        break

                    batch = data.get("result", [])
                    records.extend(batch)

                    # 检查是否还有下一页
                    result_info = data.get("result_info", {})
                    total_pages = result_info.get("total_pages", 1)
                    if page >= total_pages or not batch:
                        break
                    page += 1
                except requests.RequestException as e:
                    print(f"[!] 网络请求异常: {e}")
                    break
        return records

    def update_record_ip(self, zone_id: str, record_id: str, new_ip: str, proxied: Optional[bool] = None) -> bool:
        """
        使用 PATCH 方法修改单条解析记录的 IP。
        默认只修改 IP (content)，保留原有的 proxied (小黄云) 和 TTL 状态。
        """
        url = f"{self.base_url}/zones/{zone_id}/dns_records/{record_id}"
        payload = {"content": new_ip}
        if proxied is not None:
            payload["proxied"] = proxied

        try:
            resp = self.session.patch(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("success", False)
        except requests.RequestException as e:
            print(f"[!] 更新记录 {record_id} 失败: {e}")
            return False


def load_config() -> dict:
    """读取本地保存的配置文件"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(config: dict):
    """保存配置到本地，并在 Linux/macOS 下设置 600 权限保障安全"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    if os.name == "posix":
        os.chmod(CONFIG_FILE, 0o600)


def print_records_table(all_records: List[Dict]):
    """格式化打印解析记录表"""
    if not all_records:
        print("\n[*] 未查询到任何解析记录。")
        return

    print("\n" + "=" * 95)
    print(f"{'序号':<5} | {'主域名 (Zone)':<16} | {'完整域名 (Name)':<28} | {'类型':<5} | {'当前解析 IP':<18} | {'代理状态'}")
    print("-" * 95)
    for idx, item in enumerate(all_records):
        proxied_str = "开启 (CDN)" if item["proxied"] else "仅 DNS (直连)"
        print(f"{idx:<5} | {item['zone_name']:<16} | {item['name']:<28} | {item['type']:<5} | {item['content']:<18} | {proxied_str}")
    print("=" * 95)


def main():
    config = load_config()

    # 1. 自动读取或首次询问 API Token
    api_token = config.get("api_token", "")
    if not api_token:
        api_token = input("首次运行，请输入你的 Cloudflare API Token: ").strip()
        if not api_token:
            print("[!] Token 不能为空，退出。")
            sys.exit(1)
        config["api_token"] = api_token
        save_config(config)
        print(f"[+] Token 已自动保存至 {CONFIG_FILE}，下次运行无需再输！\n")

    cf = CloudflareDNSManager(api_token)

    # 2. 自动读取或更新常用二级域名列表
    saved_domains = config.get("domains", [])
    if saved_domains:
        prompt = f"请输入二级域名（多个用逗号分隔，直接回车默认查询上次的 {saved_domains}）: "
    else:
        prompt = "请输入要查询的二级域名（多个用逗号分隔，如 a.com, b.com）: "

    user_input = input(prompt).strip()
    if user_input:
        domains = [d.strip() for d in user_input.split(",") if d.strip()]
        config["domains"] = domains
        save_config(config)
    else:
        domains = saved_domains

    if not domains:
        print("[!] 未提供任何二级域名，退出。")
        sys.exit(1)

    # 汇总所有查询到的记录
    flat_records = []

    print("\n[*] 正在拉取 Cloudflare DNS 解析数据...")
    for domain in domains:
        zone_id = cf.get_zone_id(domain)
        if not zone_id:
            continue
        # 如果只关心 IPv4，可将 ("A", "AAAA") 改为 ("A",)
        records = cf.list_ip_records(zone_id, record_types=("A", "AAAA"))
        for r in records:
            flat_records.append({
                "zone_id": zone_id,
                "zone_name": domain,
                "record_id": r["id"],
                "name": r["name"],
                "type": r["type"],
                "content": r["content"],
                "proxied": r["proxied"],
                "ttl": r["ttl"]
            })

    print_records_table(flat_records)
    if not flat_records:
        sys.exit(0)

    # 交互式修改循环
    while True:
        print("\n请选择操作模式：")
        print("  [1] 按序号修改单条三级域名的 IP")
        print("  [2] 按子域名名称修改 IP (如输入 sub.example1.com)")
        print("  [3] 批量替换 IP (将所有指向【旧 IP】的三级域名一键改为【新 IP】)")
        print("  [4] 重新设置 API Token")
        print("  [q] 退出脚本")
        choice = input("请输入选项 [1/2/3/4/q]: ").strip().lower()

        if choice == 'q':
            break

        elif choice == '4':
            new_token = input("请输入新的 Cloudflare API Token: ").strip()
            if new_token:
                config["api_token"] = new_token
                save_config(config)
                print("[+] 新 Token 已保存，请重新运行脚本生效。")
                break

        elif choice == '1':
            idx_str = input("请输入要修改的记录【序号】: ").strip()
            if not idx_str.isdigit() or not (0 <= int(idx_str) < len(flat_records)):
                print("[!] 序号无效。")
                continue
            target = flat_records[int(idx_str)]
            new_ip = input(f"当前 [{target['name']}] 解析为 {target['content']}，请输入【新 IP】: ").strip()
            if not new_ip:
                continue
            if cf.update_record_ip(target["zone_id"], target["record_id"], new_ip):
                print(f"[+] 修改成功！{target['name']} -> {new_ip}")
                target["content"] = new_ip
            else:
                print("[-] 修改失败。")

        elif choice == '2':
            sub_name = input("请输入完整的三级域名 (如 node1.example.com): ").strip()
            matches = [r for r in flat_records if r["name"] == sub_name]
            if not matches:
                print(f"[!] 未找到域名 {sub_name} 的记录。")
                continue
            new_ip = input(f"找到 {len(matches)} 条匹配记录，请输入要更新的【新 IP】: ").strip()
            if not new_ip:
                continue
            for m in matches:
                if cf.update_record_ip(m["zone_id"], m["record_id"], new_ip):
                    print(f"[+] 修改成功！{m['name']} (原 IP: {m['content']}) -> {new_ip}")
                    m["content"] = new_ip

        elif choice == '3':
            old_ip = input("请输入需要被替换的【旧 IP】: ").strip()
            matches = [r for r in flat_records if r["content"] == old_ip]
            if not matches:
                print(f"[!] 当前列表中没有任何记录指向 {old_ip}。")
                continue
            print(f"[*] 共找到 {len(matches)} 条指向 {old_ip} 的记录：")
            for m in matches:
                print(f"    - {m['name']} ({m['zone_name']})")
            new_ip = input("请输入替换后的【新 IP】: ").strip()
            if not new_ip:
                continue
            confirm = input(f"确认将上述 {len(matches)} 条记录全部改为 {new_ip} 吗？(y/n): ").strip().lower()
            if confirm == 'y':
                for m in matches:
                    if cf.update_record_ip(m["zone_id"], m["record_id"], new_ip):
                        print(f"[+] 替换成功: {m['name']} -> {new_ip}")
                        m["content"] = new_ip

        print_records_table(flat_records)


if __name__ == "__main__":
    main()
