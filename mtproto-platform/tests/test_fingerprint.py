from src.modules.fingerprint import generate_proxy_fingerprint

def test_generate_proxy_fingerprint() -> None:
    f1 = generate_proxy_fingerprint(" MTproto", " 1.2.3.4  ", 443, "EE000 ")
    f2 = generate_proxy_fingerprint("mtproto", "1.2.3.4", 443, "ee000")
    assert f1 == f2
    
    f3 = generate_proxy_fingerprint("mtproto", "1.2.3.5", 443, "ee000")
    assert f1 != f3