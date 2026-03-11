import sys
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

SELECTORS_FILE = Path(__file__).resolve().parent / "custom_selectors.json"

def get_unique_selector_js():
    return """
    function getCssSelector(el) {
        // 先尝试通过常见属性能否唯一确定
        var basicSel = el.nodeName.toLowerCase();
        if (el.hasAttribute("onclick")) {
            return basicSel + '[onclick="' + el.getAttribute("onclick") + '"]';
        } else if (el.hasAttribute("href")) {
            return basicSel + '[href="' + el.getAttribute("href") + '"]';
        } else if (el.id) {
            return basicSel + '#' + el.id;
        }
        
        // 否则回退到层级路径
        var path = [];
        while (el && el.nodeType === Node.ELEMENT_NODE) {
            var selector = el.nodeName.toLowerCase();
            if (el.id) {
                selector += '#' + el.getAttribute('id');
                path.unshift(selector);
                break;
            } else {
                var sib = el, nth = 1;
                while (sib = sib.previousElementSibling) {
                    if (sib.nodeName.toLowerCase() == selector)
                       nth++;
                }
                if (nth != 1) selector += ":nth-of-type("+nth+")";
            }
            path.unshift(selector);
            el = el.parentNode;
        }
        return path.join(" > ");
    }
    document.addEventListener("click", function(e) {
        var sel = getCssSelector(e.target);
        window.saveSelector(sel);
    }, true);
    """

def main():
    print("========================================================")
    print("下载按钮智能定位器 (交互式)")
    print("========================================================")
    
    if len(sys.argv) < 2:
        doi = input("请输入用于测试的论文 DOI (例如 10.1063/1.481644): ").strip()
    else:
        doi = sys.argv[1]
        
    # 如果用户是从文件名复制包含_的，尝试转换成标准/
    if "_" in doi and "/" not in doi:
        doi = doi.replace("_", "/", 1)
        
    url = f"https://sci-hub.st/{doi}"
    
    print(f"\n[1] 即将打开网页: {url}")
    print("[2] 请在弹出的浏览器中，手动点击页面上正确的【下载 PDF / Save 按钮】！")
    print("   （如果页面卡住变白屏，您可以直接在浏览器地址栏里手动换一个节点，比如改成 sci-hub.ru）")
    print("[3] 点击后，本脚本会自动拦截并提取该按钮的特征代码保存。")
    print("---------------------------------------------------------")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        
        selector_captured = []
        
        def save_selector(sel):
            print(f"\n✅ [成功] 拦截到您的点击！已提取目标按钮特征: \n>> {sel}")
            selector_captured.append(sel)
            
            # Save to JSON
            selectors = []
            if SELECTORS_FILE.exists():
                with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
                    try:
                        selectors = json.load(f)
                    except Exception:
                        pass
            if sel not in selectors:
                selectors.append(sel)
            with open(SELECTORS_FILE, "w", encoding="utf-8") as f:
                json.dump(selectors, f, indent=4)
                
            print(f"✅ 已保存到 custom_selectors.json")
            print("👉 此后 PaperHarvester 会优先尝试寻找这里的按钮！")
            print("您可以直接关闭浏览器窗口了。")
            
        page.expose_function("saveSelector", save_selector)
        # 初始化脚本保证即使您手动刷新页面，点击拦截逻辑依然存在
        page.add_init_script(get_unique_selector_js())
        
        try:
            page.goto(url, timeout=15000)
        except Exception as e:
            print(f"⚠️ 初次加载失败或太慢 ({type(e).__name__})。已忽略。您可以在浏览器里自由操作！")

        try:
            print(f"⏳ 等待您的点击操作 (当前超时时间为 3 分钟)...")
            page.wait_for_timeout(180000)
        except Exception as e:
            if "TargetClosedError" not in str(type(e)):
                pass
            else:
                print("浏览器已关闭。")
            
        if not selector_captured:
            print("\n❌ 未检测到您的点击或等待超时已退出。如果没成功请重试。")

if __name__ == "__main__":
    main()
