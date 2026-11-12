// Rust support code for std.http (see http.zy and docs/stdlib.md). It is added to a program that imports the module,
// and is built with the crates listed in http.toml.
#[allow(dead_code)]
mod zylo_http {
    use std::collections::BTreeMap;
    use std::io::Read;
    use std::rc::Rc;

    type Map = BTreeMap<String, String>;
    type Handler = Rc<dyn Fn(String, String, Map, Map, String) -> (i64, Map, String)>;

    const ALLOW: &str = "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS";

    fn hex(b: u8) -> Option<u8> {
        match b {
            b'0'..=b'9' => Some(b - b'0'),
            b'a'..=b'f' => Some(b - b'a' + 10),
            b'A'..=b'F' => Some(b - b'A' + 10),
            _ => None,
        }
    }

    /// Percent-decode a URL component; `plus` turns `+` into a space (query strings).
    fn decode(s: &str, plus: bool) -> String {
        let b = s.as_bytes();
        let mut out = Vec::with_capacity(b.len());
        let mut i = 0;
        while i < b.len() {
            match b[i] {
                b'%' if i + 2 < b.len() && hex(b[i + 1]).is_some() && hex(b[i + 2]).is_some() => {
                    out.push(hex(b[i + 1]).unwrap() * 16 + hex(b[i + 2]).unwrap());
                    i += 3;
                }
                b'+' if plus => {
                    out.push(b' ');
                    i += 1;
                }
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    }

    fn split_url(url: &str) -> (String, Map) {
        let url = url.split('#').next().unwrap_or("");
        let (path, q) = match url.split_once('?') {
            Some((p, q)) => (p, q),
            None => (url, ""),
        };
        let mut query = Map::new();
        for pair in q.split('&').filter(|p| !p.is_empty()) {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            query.insert(decode(k, true), decode(v, true));
        }
        (decode(path, false), query)
    }

    fn text_response(status: u16, body: &str) -> tiny_http::Response<std::io::Cursor<Vec<u8>>> {
        let h = tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"text/plain; charset=utf-8"[..]).unwrap();
        tiny_http::Response::from_data(body.as_bytes().to_vec()).with_status_code(status).with_header(h)
    }

    fn panic_message(p: &Box<dyn std::any::Any + Send>) -> String {
        if let Some(s) = p.downcast_ref::<&str>() {
            s.to_string()
        } else if let Some(s) = p.downcast_ref::<String>() {
            s.clone()
        } else {
            String::from("unknown panic")
        }
    }

    /// Accept requests one at a time and answer each with `handler`. `max_requests == 0` means forever.
    pub fn serve(host: String, port: i64, max_requests: i64, max_body: i64, handler: Handler) -> Result<i64, String> {
        if !(0..=65535).contains(&port) {
            return Err(format!("invalid port {}", port));
        }
        let server = tiny_http::Server::http(format!("{}:{}", host, port)).map_err(|e| format!("cannot listen on {}:{}: {}", host, port, e))?;
        eprintln!("listening on http://{}", server.server_addr());
        let mut served: i64 = 0;
        while max_requests <= 0 || served < max_requests {
            let mut req = match server.recv() {
                Ok(r) => r,
                Err(e) => return Err(format!("accept failed: {}", e)),
            };
            served += 1;
            let method = req.method().as_str().to_uppercase();
            let (path, query) = split_url(req.url());
            let mut headers = Map::new();
            for h in req.headers() {
                headers.insert(h.field.as_str().as_str().to_lowercase(), h.value.as_str().to_string());
            }
            if let Some(n) = req.body_length() {
                if n as i64 > max_body {
                    let _ = req.respond(text_response(413, "Payload Too Large"));
                    continue;
                }
            }
            let mut raw = Vec::new();
            let limit = (max_body.max(0) as u64) + 1;
            if req.as_reader().take(limit).read_to_end(&mut raw).is_err() {
                let _ = req.respond(text_response(400, "Bad Request"));
                continue;
            }
            if raw.len() as i64 > max_body {
                let _ = req.respond(text_response(413, "Payload Too Large"));
                continue;
            }
            let body = String::from_utf8_lossy(&raw).into_owned();
            let h = handler.clone();
            let m = method.clone();
            let call = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || h(m, path, query, headers, body)));
            let reply = match call {
                Ok((status, hdrs, mut body)) => {
                    let mut status = status.clamp(100, 599);
                    let mut hdrs = hdrs;
                    if method == "OPTIONS" && status == 404 {
                        status = 204;
                        body = String::new();
                        hdrs.insert("Allow".to_string(), ALLOW.to_string());
                    }
                    let mut r = tiny_http::Response::from_data(body.into_bytes()).with_status_code(status as u16);
                    for (k, v) in hdrs {
                        if k.eq_ignore_ascii_case("content-length") {
                            continue; // tiny_http sets it from the body
                        }
                        if let Ok(h) = tiny_http::Header::from_bytes(k.as_bytes(), v.as_bytes()) {
                            r.add_header(h);
                        }
                    }
                    let _ = req.respond(r);
                    continue;
                }
                Err(p) => {
                    eprintln!("handler panicked: {}", panic_message(&p));
                    text_response(500, "Internal Server Error")
                }
            };
            let _ = req.respond(reply);
        }
        Ok(served)
    }

    /// One blocking request. The result is `(status, headers, body)`; an HTTP error status is a normal response.
    pub fn request(method: String, url: String, body: String, headers: Map, timeout_secs: i64) -> Result<(i64, Map, String), String> {
        let agent = ureq::AgentBuilder::new()
            .redirects(0)
            .timeout(std::time::Duration::from_secs(timeout_secs.max(1) as u64))
            .build();
        let mut req = agent.request(&method, &url);
        for (k, v) in &headers {
            req = req.set(k, v);
        }
        let outcome = if body.is_empty() && (method == "GET" || method == "HEAD" || method == "DELETE") {
            req.call()
        } else {
            req.send_string(&body)
        };
        let resp = match outcome {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r,
            Err(e) => return Err(e.to_string()),
        };
        let status = resp.status() as i64;
        let mut out = Map::new();
        for name in resp.headers_names() {
            if let Some(v) = resp.header(&name) {
                out.insert(name.to_lowercase(), v.to_string());
            }
        }
        let mut raw = Vec::new();
        resp.into_reader().take(64 * 1024 * 1024).read_to_end(&mut raw).map_err(|e| e.to_string())?;
        Ok((status, out, String::from_utf8_lossy(&raw).into_owned()))
    }
}
