# 登录状态与失败处理

浏览器正常播放不等于下载脚本可用。Chrome 无痕窗口的 Cookie 不会被 `--cookies-from-browser chrome` 读取。

如果匿名访问要求登录，让用户选择普通 Chrome 登录并授权读取，或提供自行导出的 YouTube Netscape Cookie 文件的本地路径。不要要求粘贴凭证到聊天。普通浏览器方式会由 yt-dlp 读取该配置的 Cookie 存储，不能宣称只读取 YouTube Cookie；不额外导出整个 Cookie 库。多个配置存在时，依据用户指定或浏览器资料选择已授权配置，避免轮流读取所有配置。

Cookie 是会话凭证。文件留在仓库外，建议权限 `0600`，不写入报告或日志。yt-dlp 可能刷新用户提供的 Cookie 文件。需要登录时才使用账号，低频顺序下载；持续触发登录、验证码或限流时停止，不无限重试或轮换账号。

官方依据：
- https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies
- https://github.com/yt-dlp/yt-dlp/wiki/EJS
