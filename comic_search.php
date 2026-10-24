<?php
declare(strict_types=1);

/**
 * comic.baby 搜索接口代理（PHP >= 7.4，同时兼容 8.x）
 * ------------------------------------------------------------------
 * 上游接口（无需任何签名 / Token / Cookie）：
 *   GET /api/catalog/search?q=&page=&pageSize=&sort=[&tag=&category=]
 *   GET /api/catalog/search/suggest?q=&limit=
 *
 * 核心行为：
 *   一次调用并发请求 search + suggest，结果合并进同一个 data 数组。
 *
 * 用法：
 *   /comic_search.php?q=350234&limit=30               → 默认：两个接口并发 + 合并
 *   /comic_search.php?q=350234&limit=30&mode=search   → 只要列表搜索
 *   /comic_search.php?q=35234&limit=6&mode=suggest    → 只要联想
 *   /comic_search.php?q=斗破&limit=10&page=2&sort=popular
 *   /comic_search.php?q=350234&raw=1                  → 返回两个接口的原始结构
 *   /comic_search.php?q=350234&slim=1                 → 统一精简字段
 *
 * 环境要求：PHP >= 7.4，curl 扩展
 */

/* ============================ 配置区 ============================ */

/** 上游站点 */
define('UPSTREAM_BASE', 'https://comic.baby');

/** 上游列表接口 */
define('EP_SEARCH', '/api/catalog/search');

/** 上游联想接口 */
define('EP_SUGGEST', '/api/catalog/search/suggest');

/** 伪装 UA（上游不校验，但带上更稳妥） */
define('UPSTREAM_UA', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    . '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36');

/** 连接超时 / 总超时（秒） */
define('CONNECT_TIMEOUT', 8);
define('TOTAL_TIMEOUT', 15);

/** 失败重试次数（不含首次） */
define('MAX_RETRY', 2);

/** 缓存目录与默认 TTL（秒） */
define('CACHE_TTL_SEARCH', 60);
define('CACHE_TTL_SUGGEST', 120);

/** 上游硬上限 */
define('MAX_PAGE_SIZE', 50);
define('MAX_SUGGEST_LIMIT', 20);

/**
 * 极端兜底：证书商店彻底不可用（系统无 CA、目录也放不了 cacert.pem）时，
 * 是否允许关闭 SSL 校验。
 * 默认 false —— 只有在确认服务器没有任何可用证书时才手动改成 true。
 */
define('ALLOW_INSECURE_SSL', false);

/* ---- 兜底：任何 stray 输出（Warning / Notice / BOM）都不能混进 JSON ---- */
ob_start();
define('OBF_OWNED', true);

/* ============================ 通用工具 ============================ */

/**
 * open_basedir 允许的目录前缀
 * 返回空数组 = 没有启用 open_basedir 限制
 */
function basedir_prefixes(): array
{
    static $prefixes = null;
    if ($prefixes !== null) {
        return $prefixes;
    }

    $prefixes = array();
    $open = ini_get('open_basedir');
    if (!is_string($open) || trim($open) === '') {
        return $prefixes;
    }

    $sep = defined('PATH_SEPARATOR') ? PATH_SEPARATOR : ':';
    foreach (explode($sep, $open) as $p) {
        $p = trim($p);
        if ($p === '') {
            continue;
        }
        $p = rtrim(str_replace('\\', '/', $p), '/');
        $prefixes[] = ($p === '') ? '/' : $p;
    }

    return $prefixes;
}

/**
 * 路径是否落在 open_basedir 允许范围内
 *
 * 为什么需要它：open_basedir 生效时，对范围外的路径调用 is_file() 会抛 Warning
 * （"open_basedir restriction in effect"）。如果 display_errors 开着，
 * 这些 Warning 会直接打到响应体里，把 JSON 弄坏。所以在探测前先自己判断。
 */
function path_allowed(string $path): bool
{
    $prefixes = basedir_prefixes();
    if (empty($prefixes)) {
        return true;
    }

    $path = trim($path);
    if ($path === '') {
        return false;
    }

    $target = str_replace('\\', '/', $path);
    if ($target[0] !== '/') {
        // Windows 盘符路径（C:/...）或相对路径，先按绝对处理
        if (!preg_match('#^[A-Za-z]:/#', $target)) {
            $target = str_replace('\\', '/', __DIR__) . '/' . $target;
        }
    }

    $isWin = (DIRECTORY_SEPARATOR === '\\');
    foreach ($prefixes as $p) {
        $pfx = ($p === '/') ? '/' : $p . '/';
        if ($isWin) {
            $a = strtolower($target);
            $b = strtolower($pfx);
            $base = strtolower($p);
        } else {
            $a = $target;
            $b = $pfx;
            $base = $p;
        }
        if ($a === $base || strpos($a, $b) === 0) {
            return true;
        }
    }

    return false;
}

/**
 * 静默版 is_file()：不在 open_basedir 范围内的路径直接返回 false，不产生 Warning
 */
function safe_is_file(string $path): bool
{
    if (!path_allowed($path)) {
        return false;
    }
    return @is_file($path);   // @ 兜底：符号链接 / realpath 差异仍可能触发
}

/**
 * CA 根证书候选路径
 * （用函数而非常量数组，避免依赖常量表达式对 __DIR__ 的支持差异）
 */
function ca_candidates(): array
{
    $list = array(__DIR__ . '/cacert.pem');

    if (DIRECTORY_SEPARATOR === '\\') {
        $list[] = 'C:/Program Files/Common Files/SSL/cacert.pem';
        $list[] = 'C:/Windows/System32/curl-ca-bundle.crt';
    } else {
        $list[] = '/etc/ssl/certs/ca-certificates.crt';
        $list[] = '/etc/pki/tls/certs/ca-bundle.crt';
        $list[] = '/etc/ssl/cert.pem';
        $list[] = '/usr/local/ssl/cert.pem';
        $list[] = '/etc/ssl/certs/ca-bundle.crt';
    }

    return $list;
}

/**
 * 定位可用的 CA 证书文件
 *
 * 关键点：php.ini / 环境变量里已经指定的证书路径**不做 is_file 探测**。
 * open_basedir 只限制 PHP 自己的文件函数，libcurl 读取证书文件不受影响；
 * 用 is_file 去验证反而会抛出 Warning 污染 JSON 输出。
 * 只有我们自己猜的候选路径才做探测，而且必须走 safe_is_file()。
 */
function ca_bundle()
{
    static $resolved = false;
    static $bundle   = null;

    if ($resolved) {
        return $bundle;
    }
    $resolved = true;
    $bundle   = null;

    /* 1. 环境变量 / php.ini 已显式指定（直接信任，不 stat） */
    $env = getenv('CURL_CA_BUNDLE');
    if (is_string($env) && trim($env) !== '') {
        $bundle = trim($env);
        return $bundle;
    }

    foreach (array('curl.cainfo', 'openssl.cafile') as $ini) {
        $p = ini_get($ini);
        if (is_string($p) && trim($p) !== '') {
            $bundle = trim($p);
            return $bundle;
        }
    }

    /* 2. 同目录 cacert.pem 及常见系统路径（只探测 open_basedir 允许的路径） */
    foreach (ca_candidates() as $p) {
        if (safe_is_file($p)) {
            $bundle = $p;
            return $bundle;
        }
    }

    /* 3. PHP 安装目录 */
    $default = PHP_BINDIR . '/cacert.pem';
    if (safe_is_file($default)) {
        $bundle = $default;
        return $bundle;
    }

    return null;
}

/* ============================ 响应层 ============================ */

/**
 * meta 的固定骨架
 * 无论成功还是失败，meta 都至少包含这几个键，保证响应结构不随结果变化
 */
function meta_skeleton(): array
{
    return array(
        'q'          => '',
        'mode'       => '',
        'count'      => 0,
        'duplicated' => 0,
        'cached'     => false,
        'ms'         => 0,
    );
}

/**
 * 统一响应信封
 * 任何情况下都只有 code / msg / data / meta 四个键：
 *   code  0=成功，其它=对应 HTTP 状态码
 *   msg   唯一的提示信息出口，失败原因全部写在这里
 *   data  永远是数组（失败时为空数组，不会变成 null）
 *   meta  永远是对象，至少包含 meta_skeleton() 的键
 *
 * @param int    $code 业务状态码
 * @param string $msg  提示信息
 * @param mixed  $data 数据，null 会被转成空数组
 * @param array  $meta 附加元信息
 */
function envelope(int $code, string $msg, $data = null, array $meta = array()): array
{
    return array(
        'code' => $code,
        'msg'  => $msg,
        'data' => $data === null ? array() : $data,
        'meta' => array_merge(meta_skeleton(), $meta),
    );
}

/**
 * 输出 JSON 并终止
 *
 * @param int    $http     HTTP 状态码
 * @param array  $body     已经由 envelope() 组装好的响应体
 * @param bool   $jsonp    是否 JSONP 输出
 * @param string $callback JSONP 回调名
 */
function respond(int $http, array $body, bool $jsonp = false, string $callback = '')
{
    // 失败路径的调用点拿不到 $jsonp/$callback，这里统一回落到 GET 参数，
    // 保证成功与失败的输出格式也完全一致（JSONP 场景不会突然变成裸 JSON）
    if (!$jsonp && isset($_GET['jsonp']) && $_GET['jsonp'] === '1') {
        $jsonp = true;
    }
    if ($callback === '' && isset($_GET['callback']) && is_string($_GET['callback'])) {
        $callback = trim($_GET['callback']);
    }

    http_response_code($http);
    header('Content-Type: application/json; charset=utf-8');
    header('Access-Control-Allow-Origin: *');
    header('Access-Control-Allow-Methods: GET, OPTIONS');
    header('Access-Control-Allow-Headers: Content-Type');
    header('Cache-Control: no-store');

    $json = json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    if ($json === false) {
        // 极端情况（如非法 UTF-8）：仍然保持四键结构，错误信息写进 msg
        $fallback = envelope(500, 'json encode failed: ' . json_last_error_msg());
        $json = (string) json_encode($fallback, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    }

    if ($jsonp && $callback !== '' && preg_match('/^[A-Za-z_$][A-Za-z0-9_$.]*$/', $callback)) {
        header('Content-Type: application/javascript; charset=utf-8');
        $payload = $callback . '(' . $json . ');';
    } else {
        $payload = $json;
    }

    // 丢弃缓冲里的任何意外输出（Warning / Notice），保证响应体是纯 JSON
    if (defined('OBF_OWNED')) {
        while (ob_get_level() > 0) {
            ob_end_clean();
        }
    }

    echo $payload;
    exit;
}

/**
 * 失败响应
 * 与成功响应保持完全相同的键结构，失败原因只写进 msg
 *
 * @param int    $http     HTTP 状态码
 * @param string $msg      失败原因
 * @param array  $meta     附加元信息（可选，仍会被骨架补全）
 */
function fail(int $http, string $msg, array $meta = array())
{
    respond($http, envelope($http, $msg, array(), $meta));
}

/* ============================ 缓存 ============================ */

function cache_dir(): string
{
    return __DIR__ . '/.cache';
}

function cache_path(string $url): string
{
    return cache_dir() . '/' . md5($url) . '.json';
}

function cache_get(string $url, int $ttl)
{
    if ($ttl <= 0) {
        return null;
    }
    $file = cache_path($url);
    if (!safe_is_file($file)) {
        return null;
    }
    $mtime = @filemtime($file);
    if ($mtime === false || (time() - (int) $mtime) >= $ttl) {
        return null;
    }
    $raw = @file_get_contents($file);
    return ($raw !== false && $raw !== '') ? $raw : null;
}

function cache_put(string $url, string $body)
{
    $dir = cache_dir();
    if (!@is_dir($dir) && !@mkdir($dir, 0775, true) && !@is_dir($dir)) {
        return;
    }
    $tmp = cache_path($url) . '.' . getmypid() . '.tmp';
    if (@file_put_contents($tmp, $body) !== false) {
        @rename($tmp, cache_path($url));
    } else {
        @unlink($tmp);
    }
}

function cache_purge(): int
{
    $n = 0;
    $files = glob(cache_dir() . '/*.json');
    if (is_array($files)) {
        foreach ($files as $f) {
            if (@unlink($f)) {
                $n++;
            }
        }
    }
    return $n;
}

/* ============================ HTTP 层 ============================ */

/**
 * 构建单个 curl 句柄（统一请求头 / 证书 / 压缩 / HTTP2）
 *
 * @param string $url
 * @param string $key         结果键，用于回填响应头
 * @param array  $headerStore 引用，收集各句柄响应头
 * @param string $ssl         auto=用探测到的证书包; default=交给 libcurl 默认证书; off=不校验
 * @return resource|\CurlHandle
 */
function make_handle(string $url, string $key, array &$headerStore, string $ssl = 'auto')
{
    $headers = array(
        'Accept: application/json, text/plain, */*',
        'Accept-Language: zh-CN,zh;q=0.9,en;q=0.8',
        'Referer: ' . UPSTREAM_BASE . '/',
        'Origin: ' . UPSTREAM_BASE,
        'Sec-Fetch-Dest: empty',
        'Sec-Fetch-Mode: cors',
        'Sec-Fetch-Site: same-origin',
    );

    $headerStore[$key] = array();

    $ch = curl_init();
    $opts = array(
        CURLOPT_URL            => $url,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_CONNECTTIMEOUT => CONNECT_TIMEOUT,
        CURLOPT_TIMEOUT        => TOTAL_TIMEOUT,
        CURLOPT_USERAGENT      => UPSTREAM_UA,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_ENCODING       => '',
        CURLOPT_HTTP_VERSION   => CURL_HTTP_VERSION_2TLS,
        CURLOPT_HEADER         => false,
    );

    if ($ssl === 'off') {
        $opts[CURLOPT_SSL_VERIFYPEER] = false;
        $opts[CURLOPT_SSL_VERIFYHOST] = 0;
    } else {
        $opts[CURLOPT_SSL_VERIFYPEER] = true;
        $opts[CURLOPT_SSL_VERIFYHOST] = 2;
        if ($ssl === 'auto') {
            $ca = ca_bundle();
            if ($ca !== null) {
                $opts[CURLOPT_CAINFO] = $ca;
            }
        }
        // $ssl === 'default'：不指定 CAINFO，走 libcurl 编译内置的默认证书位置
    }

    curl_setopt_array($ch, $opts);

    curl_setopt($ch, CURLOPT_HEADERFUNCTION, function ($ch, $line) use (&$headerStore, $key) {
        $len = strlen($line);
        $pos = strpos($line, ':');
        if ($pos !== false) {
            $name  = strtolower(trim(substr($line, 0, $pos)));
            $value = trim(substr($line, $pos + 1));
            $headerStore[$key][$name] = $value;
        }
        return $len;
    });

    return $ch;
}

/**
 * 一轮并发请求
 *
 * @param array $jobs array<string, array{url:string, ssl?:string}>
 * @return array<string, array{code:int,body:string,err:string,errno:int,ms:int,headers:array}>
 */
function multi_round(array $jobs): array
{
    $mh          = curl_multi_init();
    $handles     = array();
    $headerStore = array();
    $results     = array();

    $t0 = microtime(true);

    foreach ($jobs as $key => $job) {
        $ssl  = isset($job['ssl']) ? (string) $job['ssl'] : 'auto';
        $ch   = make_handle($job['url'], $key, $headerStore, $ssl);
        curl_multi_add_handle($mh, $ch);
        $handles[$key] = $ch;
    }

    $active = null;
    do {
        $status = curl_multi_exec($mh, $active);
        if ($active > 0) {
            if (curl_multi_select($mh, 1.0) === -1) {
                usleep(10000);
            }
        }
    } while ($active > 0 && $status === CURLM_OK);

    $elapsed = (int) round((microtime(true) - $t0) * 1000);

    /*
     * 多路模式下 curl_errno()/curl_error() 有时拿不到真实错误
     * （例如 CAINFO 指向的证书文件读不到：单句柄返回 77，多句柄却返回 0，
     *   HTTP 状态码也是 0）。这里从完成队列里补一次真实 CURLcode。
     */
    $done = array();
    while (($msg = curl_multi_info_read($mh)) !== false) {
        if (isset($msg['msg']) && $msg['msg'] === CURLMSG_DONE && isset($msg['handle'])) {
            $done[] = array('handle' => $msg['handle'], 'result' => (int) $msg['result']);
        }
    }

    foreach ($handles as $key => $ch) {
        $body   = curl_multi_getcontent($ch);
        $errno  = (int) curl_errno($ch);
        $errMsg = (string) curl_error($ch);

        if ($errno === 0) {
            foreach ($done as $d) {
                if ($d['handle'] === $ch && $d['result'] !== 0) {
                    $errno  = $d['result'];
                    $errMsg = function_exists('curl_strerror')
                        ? (string) curl_strerror($errno)
                        : ('curl error ' . $errno);
                    break;
                }
            }
        }

        $results[$key] = array(
            'code'    => (int) curl_getinfo($ch, CURLINFO_HTTP_CODE),
            'body'    => is_string($body) ? $body : '',
            'err'     => $errMsg,
            'errno'   => $errno,
            'ms'      => $elapsed,
            'headers' => isset($headerStore[$key]) ? $headerStore[$key] : array(),
        );
        curl_multi_remove_handle($mh, $ch);
        curl_close($ch);
    }

    curl_multi_close($mh);

    return $results;
}

/**
 * 是否属于「证书链」类失败
 * 60=对端证书无法用本地 CA 验证  77=CA 证书文件读不到
 * 83=证书签发者校验失败          51=对端证书已过期/无效
 */
function is_ssl_cert_error(int $errno): bool
{
    return in_array($errno, array(51, 60, 77, 83), true);
}

/**
 * 证书失败时的降级路径：
 *   auto    → 用探测到的 cacert.pem 失败 → 退回 libcurl 默认证书位置
 *   default → 系统证书也失败 → 仅在 ALLOW_INSECURE_SSL=true 时关闭校验
 *
 * @return string|false 下一个模式，false 表示无可降级
 */
function next_ssl_mode(string $current)
{
    if ($current === 'auto') {
        return 'default';
    }
    if ($current === 'default' && ALLOW_INSECURE_SSL === true) {
        return 'off';
    }
    return false;
}

/**
 * 并发请求多个 URL，自带缓存与重试
 *
 * @param array $jobs array<string, array{url:string, ttl:int}>
 * @return array{results: array, ms: int, cachedAll: bool}
 */
function multi_fetch(array $jobs): array
{
    $results = array();
    $pending = array();
    $hit     = 0;
    $wall    = 0;

    // 1. 先查缓存
    foreach ($jobs as $key => $job) {
        $cached = cache_get($job['url'], (int) $job['ttl']);
        if ($cached !== null) {
            $results[$key] = array(
                'code' => 200, 'body' => $cached, 'err' => '', 'errno' => 0,
                'ms' => 0, 'headers' => array(),
            );
            $hit++;
        } else {
            if (!isset($job['ssl'])) {
                $job['ssl'] = 'auto';
            }
            $pending[$key] = $job;
        }
    }

    // 2. 并发请求 + 失败重试（证书降级额外给 2 次机会，不占用常规重试次数）
    $round  = 0;
    $budget = MAX_RETRY + 1;

    while (!empty($pending) && $round < $budget) {
        if ($round > 0) {
            usleep((int) (300000 * $round * (1 + mt_rand(0, 100) / 100)));
        }

        $out      = multi_round($pending);
        $maxMs    = 0;
        $escalate = 0;

        foreach ($out as $key => $r) {
            $ok    = ($r['code'] >= 200 && $r['code'] < 300 && $r['body'] !== '');
            $fatal = ($r['code'] >= 400 && $r['code'] < 500 && $r['code'] !== 429);

            if ($ok || $fatal) {
                $results[$key] = $r;
                unset($pending[$key]);
                if ($r['ms'] > $maxMs) {
                    $maxMs = $r['ms'];
                }
                continue;
            }

            /* 证书问题：换一套证书信任链再试，不计入常规重试次数 */
            if ($r['errno'] > 0 && is_ssl_cert_error((int) $r['errno'])) {
                $next = next_ssl_mode((string) $pending[$key]['ssl']);
                if ($next !== false) {
                    $pending[$key]['ssl'] = $next;
                    $escalate += 2;
                    if ($r['ms'] > $maxMs) {
                        $maxMs = $r['ms'];
                    }
                    continue;
                }
            }

            if ($round >= MAX_RETRY) {
                $results[$key] = $r;   // 已达重试上限，原样返回失败态
                unset($pending[$key]);
                if ($r['ms'] > $maxMs) {
                    $maxMs = $r['ms'];
                }
            }
        }

        $wall += $maxMs;
        $budget += $escalate;
        $round++;
    }

    // 3. 落缓存
    foreach ($results as $key => $r) {
        if ($r['code'] >= 200 && $r['code'] < 300 && $r['body'] !== '') {
            cache_put($jobs[$key]['url'], $r['body']);
        }
    }

    return array(
        'results'   => $results,
        'ms'        => $wall,
        'cachedAll' => ($hit === count($jobs) && count($jobs) > 0),
    );
}

/**
 * 把一次失败的结果转成便于排查的提示（写进 msg / meta.errors）
 */
function upstream_err_msg(array $r): string
{
    if ($r['err'] !== '') {
        $msg = preg_replace('/\s+/u', ' ', trim($r['err']));
        if (isset($r['errno']) && is_ssl_cert_error((int) $r['errno'])) {
            $msg .= '（证书链不可用：把 cacert.pem 放到本文件同目录，或在 php.ini 里配置 curl.cainfo）';
        }
        return $msg;
    }
    return '上游返回 HTTP ' . $r['code'];
}

/* ============================ 参数处理 ============================ */

function param(string $key, string $default = ''): string
{
    if (!isset($_GET[$key])) {
        return $default;
    }
    $v = $_GET[$key];
    return is_string($v) ? trim($v) : $default;
}

/**
 * 前端 sort 中文标签 → 上游可用值
 * 实测仅 updatedAt / popular 真正生效，其余会被上游静默回退
 */
function normalize_sort(string $sort): string
{
    $map = array(
        '最新的'    => 'updatedAt',
        '最新'      => 'updatedAt',
        'latest'    => 'updatedAt',
        'updatedAt' => 'updatedAt',
        '最多点阅'  => 'popular',
        '最高人气'  => 'popular',
        '最多浏览'  => 'popular',
        'popular'   => 'popular',
        'views'     => 'popular',
        '最多爱心'  => 'favorites',
        'favorites' => 'favorites',
        '最多评论'  => 'comments',
        'comments'  => 'comments',
    );

    if ($sort === '') {
        return 'updatedAt';
    }
    if (isset($map[$sort])) {
        return $map[$sort];
    }
    return preg_match('/^[A-Za-z][A-Za-z0-9_]{0,31}$/', $sort) ? $sort : 'updatedAt';
}

/**
 * 由作品 id 拼出详情页地址
 * 例：rohox7c61v2r57f3eccmrikl → https://comic.baby/work/rohox7c61v2r57f3eccmrikl
 */
function work_url($id)
{
    $id = is_string($id) ? trim($id) : '';
    return $id !== '' ? UPSTREAM_BASE . '/work/' . $id : null;
}

/**
 * 给条目补一列 url（已存在则不动，只补缺失的）
 */
function with_url(array $it): array
{
    if (!array_key_exists('url', $it) || $it['url'] === null || $it['url'] === '') {
        $it['url'] = work_url(isset($it['id']) ? $it['id'] : '');
    }
    return $it;
}

/**
 * 统一条目结构（search 与 suggest 的原始结构不同，合并时必须归一）
 */
function normalize_item(array $it, string $source): array
{
    $id = isset($it['id']) ? (string) $it['id'] : '';

    $tags = array();
    if (isset($it['tags']) && is_array($it['tags'])) {
        foreach ($it['tags'] as $t) {
            if (is_array($t)) {
                if (isset($t['name']) && $t['name'] !== '') {
                    $tags[] = (string) $t['name'];
                }
            } elseif (is_string($t) && $t !== '') {
                $tags[] = $t;
            }
        }
    }

    $author = '';
    if (isset($it['displayAuthorName']) && $it['displayAuthorName'] !== '') {
        $author = (string) $it['displayAuthorName'];
    } elseif (isset($it['authorName']) && $it['authorName'] !== '') {
        $author = (string) $it['authorName'];
    }

    $categories = array();
    if (isset($it['categories']) && is_array($it['categories'])) {
        foreach ($it['categories'] as $c) {
            if (is_array($c) && isset($c['name'])) {
                $categories[] = (string) $c['name'];
            } elseif (is_string($c)) {
                $categories[] = $c;
            }
        }
    }

    return array(
        'id'           => $id,
        'serialCode'   => isset($it['serialCode']) ? $it['serialCode'] : null,
        'title'        => isset($it['title']) ? (string) $it['title'] : '',
        'authorName'   => $author,
        'coverImageUrl' => isset($it['coverImageUrl']) ? (string) $it['coverImageUrl'] : '',
        'summary'      => isset($it['summary']) ? (string) $it['summary'] : '',
        'status'       => isset($it['status']) ? $it['status'] : null,
        'serialStatus' => isset($it['serialStatus']) ? $it['serialStatus'] : null,
        'chapterCount' => isset($it['chapterCount']) ? (int) $it['chapterCount'] : null,
        'popularity'   => isset($it['popularity']) ? (int) $it['popularity'] : null,
        'likes'        => isset($it['likes']) ? (int) $it['likes'] : null,
        'tags'         => $tags,
        'categories'   => $categories,
        'updatedAt'    => isset($it['updatedAt']) ? $it['updatedAt'] : null,
        'url'          => work_url($id),
        '_source'      => $source,
    );
}

/**
 * 解码上游响应体
 *
 * @return array{ok:bool, data:mixed, err:string}
 */
function decode_body(string $body): array
{
    $data = json_decode($body, true);
    if (json_last_error() !== JSON_ERROR_NONE) {
        return array('ok' => false, 'data' => null, 'err' => json_last_error_msg());
    }
    return array('ok' => true, 'data' => $data, 'err' => '');
}

/* ============================ 主流程 ============================ */

if (isset($_SERVER['REQUEST_METHOD']) && $_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    respond(200, envelope(0, 'ok'));
}

if (!extension_loaded('curl')) {
    fail(500, '服务端未启用 curl 扩展');
}

/* ---- 清缓存 ---- */
if (param('purge') === '1') {
    $removed = cache_purge();
    respond(200, envelope(0, 'cache purged', array(), array('purged' => $removed)));
}

/* ---- 入参 ---- */
$q         = param('q');
$modeRaw   = strtolower(param('mode'));
$typeRaw   = strtolower(param('type'));
$raw       = param('raw') === '1';
$slim      = param('slim') === '1';
$debug     = param('debug') === '1';
$jsonp     = param('jsonp') === '1';
$callback  = param('callback');
$ttlParam  = param('ttl');
$tag       = param('tag');
$category  = param('category');
$page      = max(1, (int) param('page', '1'));
$sortInput = param('sort');

/* 兼容旧参数 type=list|suggest|both */
$mode = $modeRaw;
if ($mode === '') {
    if ($typeRaw === 'suggest') {
        $mode = 'suggest';
    } elseif ($typeRaw === 'search' || $typeRaw === 'list') {
        $mode = 'search';
    } elseif ($typeRaw === 'both') {
        $mode = 'both';
    } else {
        $mode = 'both';
    }
}
if ($mode === 'list') {
    $mode = 'search';
}
if ($mode !== 'search' && $mode !== 'suggest') {
    $mode = 'both';
}

if ($q === '') {
    fail(400, '缺少参数 q（搜索关键词）', array('q' => $q, 'mode' => $mode));
}
if (mb_strlen($q, 'UTF-8') > 100) {
    fail(400, '参数 q 过长（最多 100 字符）', array('q' => $q, 'mode' => $mode));
}

/* ---- limit 分配 ---- */
$limitInput   = param('limit');
$suggestInput = param('suggestLimit');

if ($mode === 'suggest') {
    // 单联想模式：limit 就是联想条数（与旧版 type=suggest 行为一致）
    $suggestLimit = $limitInput === '' ? 6 : (int) $limitInput;
    $pageSize     = 0;
} else {
    $pageSize     = $limitInput === '' ? 30 : (int) $limitInput;
    $suggestLimit = $suggestInput === '' ? 6 : (int) $suggestInput;
}
$suggestLimit = max(1, min(MAX_SUGGEST_LIMIT, $suggestLimit));
$pageSize     = $pageSize > 0 ? max(1, min(MAX_PAGE_SIZE, $pageSize)) : 0;

/* ---- 组装请求 ---- */
$jobs  = array();
$qInfo = array();

if ($mode === 'search' || $mode === 'both') {
    $searchQuery = array(
        'q'        => $q,
        'page'     => $page,
        'pageSize' => $pageSize > 0 ? $pageSize : 30,
        'sort'     => normalize_sort($sortInput),
    );
    if ($tag !== '') {
        $searchQuery['tag'] = $tag;
    }
    if ($category !== '') {
        $searchQuery['category'] = $category;
    }
    $searchUrl = UPSTREAM_BASE . EP_SEARCH . '?' . http_build_query($searchQuery, '', '&', PHP_QUERY_RFC3986);

    $jobs['search'] = array(
        'url' => $searchUrl,
        'ttl' => $ttlParam === '' ? CACHE_TTL_SEARCH : max(0, (int) $ttlParam),
    );
    $qInfo['search'] = array('url' => $searchUrl, 'query' => $searchQuery);
}

if ($mode === 'suggest' || $mode === 'both') {
    $suggestQuery = array('q' => $q, 'limit' => $suggestLimit);
    $suggestUrl = UPSTREAM_BASE . EP_SUGGEST . '?' . http_build_query($suggestQuery, '', '&', PHP_QUERY_RFC3986);

    $jobs['suggest'] = array(
        'url' => $suggestUrl,
        'ttl' => $ttlParam === '' ? CACHE_TTL_SUGGEST : max(0, (int) $ttlParam),
    );
    $qInfo['suggest'] = array('url' => $suggestUrl, 'query' => $suggestQuery);
}

/* ---- 并发请求 ---- */
$fetch   = multi_fetch($jobs);
$results = $fetch['results'];

/* ---- 解析 ---- */
$searchData = null;
$suggestData = null;
$errors     = array();
$sections   = array();

if (isset($results['search'])) {
    $r = $results['search'];
    $decoded = decode_body($r['body']);
    if ($r['code'] === 200 && $decoded['ok'] && is_array($decoded['data'])) {
        $searchData = $decoded['data'];
        $sections['search'] = array(
            'total'    => isset($searchData['total']) ? (int) $searchData['total'] : 0,
            'page'     => isset($searchData['page']) ? (int) $searchData['page'] : $page,
            'pageSize' => isset($searchData['pageSize']) ? (int) $searchData['pageSize'] : $pageSize,
            'count'    => isset($searchData['items']) && is_array($searchData['items']) ? count($searchData['items']) : 0,
            'cached'   => $r['ms'] === 0,
        );
    } else {
        $errors['search'] = upstream_err_msg($r);
    }
}

if (isset($results['suggest'])) {
    $r = $results['suggest'];
    $decoded = decode_body($r['body']);
    if ($r['code'] === 200 && $decoded['ok'] && is_array($decoded['data'])) {
        $suggestData = $decoded['data'];
        $sections['suggest'] = array(
            'count'  => count($suggestData),
            'cached' => $r['ms'] === 0,
        );
    } else {
        $errors['suggest'] = upstream_err_msg($r);
    }
}

/* 两个都失败才报错；部分失败降级为部分可用 */
if ($searchData === null && $suggestData === null) {
    $failMeta = array(
        'q'    => $q,
        'mode' => $mode,
    );
    if ($debug) {
        foreach ($qInfo as $k => $v) {
            $failMeta['upstream_' . $k] = $v['url'];
        }
        $failMeta['errors'] = $errors;
    }
    fail(502, '上游请求失败：' . implode(' | ', array_values($errors)), $failMeta);
}

$searchItems  = ($searchData !== null && isset($searchData['items']) && is_array($searchData['items']))
    ? $searchData['items'] : array();
$suggestItems = ($suggestData !== null && is_array($suggestData)) ? $suggestData : array();

$msg = empty($errors) ? 'ok' : 'partial';

/* ---- raw 模式：返回上游原始结构（仍套同一层信封） ---- */
if ($raw) {
    if ($mode === 'both') {
        $rawData = array(
            'search'  => $searchData === null ? array() : $searchData,
            'suggest' => $suggestData === null ? array() : $suggestData,
        );
    } else {
        $rawData = $mode === 'search'
            ? ($searchData === null ? array() : $searchData)
            : $suggestItems;
    }

    $rawMeta = array('q' => $q, 'mode' => $mode, 'raw' => true);
    if (!empty($errors)) {
        $rawMeta['partial'] = true;
        $rawMeta['errors']  = $errors;
    }
    if ($debug) {
        foreach ($qInfo as $k => $v) {
            $rawMeta['upstream_' . $k] = $v['url'];
        }
    }

    respond(200, envelope(0, $msg, $rawData, $rawMeta), $jsonp, $callback);
}

/* ---- 合并：search 在前，suggest 去重后追加 ---- */
$merged      = array();
$seen        = array();
$duplicated  = 0;

foreach ($searchItems as $it) {
    if (!is_array($it)) {
        continue;
    }
    $id = isset($it['id']) ? (string) $it['id'] : '';
    if ($id !== '') {
        if (isset($seen[$id])) {
            continue;
        }
        $seen[$id] = true;
    }
    $merged[] = ($slim || $mode === 'both') ? normalize_item($it, 'search') : with_url($it);
}

foreach ($suggestItems as $it) {
    if (!is_array($it)) {
        continue;
    }
    $id = isset($it['id']) ? (string) $it['id'] : '';
    if ($id !== '' && isset($seen[$id])) {
        $duplicated++;
        continue;
    }
    if ($id !== '') {
        $seen[$id] = true;
    }
    $merged[] = normalize_item($it, 'suggest');
}

/* ---- 仅 search 且未要求 slim 时，保持上游原样结构（向后兼容），同样补一列 url ---- */
if ($mode === 'search' && !$slim && $searchData !== null) {
    $merged = array();
    foreach ($searchItems as $it) {
        if (is_array($it)) {
            $merged[] = with_url($it);
        }
    }
}

/* ---- meta ---- */
$meta = array(
    'q'             => $q,
    'mode'          => $mode,
    'count'         => count($merged),
    'duplicated'    => $duplicated,
    'cached'        => $fetch['cachedAll'],
    'ms'            => $fetch['ms'],
);

if ($mode === 'search' || $mode === 'both') {
    $meta['pageSize']    = $pageSize > 0 ? $pageSize : 30;
    $meta['page']        = $page;
    $meta['sort']        = isset($qInfo['search']) ? $qInfo['search']['query']['sort'] : 'updatedAt';
    $meta['searchTotal'] = isset($sections['search']['total']) ? $sections['search']['total'] : 0;
    $meta['searchCount'] = isset($sections['search']['count']) ? $sections['search']['count'] : 0;
    $meta['hasMore']     = ($meta['page'] * $meta['pageSize']) < $meta['searchTotal'];
}
if ($mode === 'suggest' || $mode === 'both') {
    $meta['suggestLimit'] = $suggestLimit;
    $meta['suggestCount'] = isset($sections['suggest']['count']) ? $sections['suggest']['count'] : 0;
}
if (!empty($errors)) {
    $meta['partial'] = true;
    $meta['errors']  = $errors;
}
if ($debug) {
    foreach ($qInfo as $k => $v) {
        $meta['upstream_' . $k] = $v['url'];
    }
    $remaining = array();
    foreach ($results as $k => $r) {
        if (isset($r['headers']['x-ratelimit-remaining'])) {
            $remaining[$k] = $r['headers']['x-ratelimit-remaining'];
        }
    }
    if (!empty($remaining)) {
        $meta['rate_remaining'] = $remaining;
    }
}

respond(200, envelope(0, $msg, $merged, $meta), $jsonp, $callback);
