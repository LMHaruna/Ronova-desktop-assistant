using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]

namespace RonovaInstaller
{
    internal sealed class Release
    {
        public readonly string Folder, Hash, Url;
        public readonly long DownloadSize, InstalledSize;

        public Release(string folder, string hash, long downloadSize, long installedSize, string url)
        {
            Folder = folder;
            Hash = hash;
            DownloadSize = downloadSize;
            InstalledSize = installedSize;
            Url = url;
        }

        public static Release Read()
        {
            using (var reader = new StreamReader(Assembly.GetExecutingAssembly().GetManifestResourceStream("Ronova.Release")))
            {
                return new Release(reader.ReadLine(), reader.ReadLine(),
                    long.Parse(reader.ReadLine(), CultureInfo.InvariantCulture),
                    long.Parse(reader.ReadLine(), CultureInfo.InvariantCulture), reader.ReadLine() ?? "");
            }
        }
    }

    internal static class Install
    {
        public static Uri DownloadUrl(string value)
        {
            Uri uri;
            if (!Uri.TryCreate(value, UriKind.Absolute, out uri) || uri.Scheme != Uri.UriSchemeHttps
                || uri.UserInfo.Length != 0 || uri.Fragment.Length != 0)
                throw new ArgumentException("请填写完整的 HTTPS 文件直链。");
            return uri;
        }

        public static string Destination(string value)
        {
            if (String.IsNullOrWhiteSpace(value)) throw new ArgumentException("请选择安装目录。");
            string path = Path.GetFullPath(value.Trim()).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (path.StartsWith(@"\\") || path.Length <= 3 || path.Length > 100 || path.Contains("%"))
                throw new ArgumentException("请选择本地磁盘上的短目录，例如 D:\\Ronova。");
            RequireEmpty(path);
            return path;
        }

        private static void RequireEmpty(string path)
        {
            if (File.Exists(path)) throw new IOException("安装路径已被文件占用。");
            if (Directory.Exists(path))
            {
                if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("请选择实际的安装目录。");
                using (var contents = Directory.EnumerateFileSystemEntries(path).GetEnumerator())
                    if (contents.MoveNext())
                        throw new IOException("请使用空目录安装。新版可安装到另一个目录。");
            }
        }

        public static async Task Download(HttpClient client, Uri uri, string path, long size,
            Action<long> update, CancellationToken cancel)
        {
            if (File.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("下载缓存路径已被链接占用。");
            using (var file = new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
            {
                if (file.Length > size) file.SetLength(0);
                long offset = file.Length;
                if (offset == size) return;
                for (int redirects = 0; redirects <= 5; redirects++)
                {
                    using (var request = new HttpRequestMessage(HttpMethod.Get, uri))
                    {
                        if (offset != 0) request.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(offset, null);
                        using (var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancel).ConfigureAwait(false))
                        {
                            int code = (int)response.StatusCode;
                            if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308)
                            {
                                if (response.Headers.Location == null) throw new IOException("下载地址重定向缺少目标。");
                                uri = DownloadUrl(new Uri(uri, response.Headers.Location).AbsoluteUri);
                                continue;
                            }
                            response.EnsureSuccessStatusCode();
                            var range = response.Content.Headers.ContentRange;
                            switch (response.StatusCode)
                            {
                                case HttpStatusCode.PartialContent:
                                    if (offset == 0 || range == null || range.Unit != "bytes" ||
                                        range.From != offset || range.To != size - 1 || range.Length != size)
                                        throw new IOException("服务器返回的续传范围与发布文件不一致。");
                                    break;
                                case HttpStatusCode.OK:
                                    file.SetLength(0);
                                    offset = 0;
                                    break;
                                default:
                                    throw new IOException("服务器未返回 ZIP 文件。请检查下载直链。");
                            }
                            if (response.Content.Headers.ContentLength.HasValue &&
                                response.Content.Headers.ContentLength.Value != size - offset)
                                throw new IOException("下载文件大小与本版发布包不一致。请确认链接指向指定版本的 ZIP。");
                            file.Position = offset;
                            using (var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                            {
                                var buffer = new byte[1024 * 1024];
                                var timer = Stopwatch.StartNew();
                                update(offset);
                                while (true)
                                {
                                    int read;
                                    using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel))
                                    {
                                        timeout.CancelAfter(TimeSpan.FromSeconds(45));
                                        using (timeout.Token.Register(stream.Dispose))
                                        {
                                            try
                                            {
                                                read = await stream.ReadAsync(buffer, 0, buffer.Length, timeout.Token).ConfigureAwait(false);
                                            }
                                            catch (Exception error)
                                            {
                                                if (!(error is IOException || error is ObjectDisposedException ||
                                                      error is OperationCanceledException)) throw;
                                                cancel.ThrowIfCancellationRequested();
                                                if (timeout.IsCancellationRequested) throw new TimeoutException("下载连接超时，可重新点击安装续传。");
                                                throw;
                                            }
                                            cancel.ThrowIfCancellationRequested();
                                            if (timeout.IsCancellationRequested) throw new TimeoutException("下载连接超时，可重新点击安装续传。");
                                        }
                                    }
                                    if (read == 0) break;
                                    if (file.Position + read > size) throw new IOException("服务器返回的内容超过发布文件大小。");
                                    file.Write(buffer, 0, read);
                                    if (timer.ElapsedMilliseconds >= 200)
                                    {
                                        update(file.Position);
                                        timer.Restart();
                                    }
                                }
                            }
                            if (file.Length != size) throw new IOException("下载提前结束。重新点击安装可继续下载。");
                            update(size);
                            return;
                        }
                    }
                }
            }
            throw new IOException("下载链接重定向次数过多。");
        }

        public static bool Verify(Stream file, Release release, Action<long> update, CancellationToken cancel)
        {
            if (file.Length != release.DownloadSize) return false;
            using (var sha = SHA256.Create())
            {
                var buffer = new byte[1024 * 1024];
                var timer = Stopwatch.StartNew();
                int read;
                while ((read = file.Read(buffer, 0, buffer.Length)) != 0)
                {
                    cancel.ThrowIfCancellationRequested();
                    sha.TransformBlock(buffer, 0, read, buffer, 0);
                    if (timer.ElapsedMilliseconds >= 200)
                    {
                        update(file.Position);
                        timer.Restart();
                    }
                }
                sha.TransformFinalBlock(buffer, 0, 0);
                bool valid = String.Equals(BitConverter.ToString(sha.Hash).Replace("-", ""), release.Hash,
                    StringComparison.OrdinalIgnoreCase);
                file.Position = 0;
                return valid;
            }
        }

        public static void Extract(Stream file, string staging, Release release, Action<long> update, CancellationToken cancel)
        {
            string prefix = release.Folder + "/";
            string targetPrefix = Path.GetFullPath(staging) + Path.DirectorySeparatorChar;
            long completed = 0;
            using (var zip = new ZipArchive(file, ZipArchiveMode.Read, true))
            {
                var timer = Stopwatch.StartNew();
                var buffer = new byte[1024 * 1024];
                foreach (var entry in zip.Entries)
                {
                    cancel.ThrowIfCancellationRequested();
                    if (!entry.FullName.StartsWith(prefix, StringComparison.Ordinal))
                        throw new IOException("压缩包目录与指定版本不一致。");
                    if (entry.FullName.EndsWith("/", StringComparison.Ordinal)) continue;
                    string relative = entry.FullName.Substring(prefix.Length);
                    string target = Path.GetFullPath(Path.Combine(staging, relative.Replace('/', Path.DirectorySeparatorChar)));
                    if (relative.IndexOf(':') >= 0 || !target.StartsWith(targetPrefix, StringComparison.OrdinalIgnoreCase))
                        throw new IOException("压缩包包含无效路径。");
                    if (entry.Length > release.InstalledSize - completed)
                        throw new IOException("压缩包超过预期大小。");
                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    using (var input = entry.Open())
                    using (var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write))
                    {
                        int len;
                        while ((len = input.Read(buffer, 0, buffer.Length)) != 0)
                        {
                            cancel.ThrowIfCancellationRequested();
                            output.Write(buffer, 0, len);
                            completed += len;
                            if (timer.ElapsedMilliseconds >= 200)
                            {
                                update(completed);
                                timer.Restart();
                            }
                        }
                    }
                }
            }
            if (completed != release.InstalledSize || !File.Exists(Path.Combine(staging, "package-manifest.json")))
                throw new IOException("解压文件与发布包清单不一致。");
            update(completed);
        }

        public static async Task Execute(Release release, Uri uri, string destination, Action<string, long, long> update,
            CancellationToken cancel)
        {
            string parent = Path.GetDirectoryName(destination);
            Directory.CreateDirectory(parent);
            string work = Path.Combine(parent, ".ronova-install-" + Guid.NewGuid().ToString("N"));
            string download = Path.Combine(parent, ".ronova-" + release.Hash.Substring(0, 16) + ".download-part");
            long cached = File.Exists(download) ? Math.Min(new FileInfo(download).Length, release.DownloadSize) : 0;
            if (new DriveInfo(Path.GetPathRoot(destination)).AvailableFreeSpace <
                release.InstalledSize + release.DownloadSize - cached + 8L * 1024 * 1024 * 1024)
                throw new IOException("磁盘空间不足。请为下载、解压和 Python 环境留出空间。");
            try
            {
                using (var handler = new HttpClientHandler { AllowAutoRedirect = false })
                using (var client = new HttpClient(handler) { Timeout = TimeSpan.FromMinutes(2) })
                    await Download(client, uri, download, release.DownloadSize,
                        n => update("正在下载", n, release.DownloadSize), cancel).ConfigureAwait(false);
                update("正在校验下载文件", 0, release.DownloadSize);
                bool valid;
                using (var file = File.OpenRead(download))
                {
                    valid = Verify(file, release, n => update("正在校验下载文件", n, release.DownloadSize), cancel);
                    if (valid)
                    {
                        update("正在解压", 0, release.InstalledSize);
                        Extract(file, work, release, n => update("正在解压", n, release.InstalledSize), cancel);
                    }
                }
                File.Delete(download);
                if (!valid) throw new IOException("下载文件的 SHA-256 校验失败。请确认直链后重新下载。");
                cancel.ThrowIfCancellationRequested();
                RequireEmpty(destination);
                if (Directory.Exists(destination)) Directory.Delete(destination);
                Directory.Move(work, destination);
                update("正在配置 Python 环境，请等待", 0, 0);
                SetupPython(destination);
            }
            finally
            {
                if (Directory.Exists(work)) Directory.Delete(work, true);
            }
        }

        private static void SetupPython(string folder)
        {
            var python = Path.Combine(folder, @"runtime\python\cpython-3.11.17-windows-x86_64-none\python.exe");
            string logPath = Path.Combine(folder, "install.log");
            var info = new ProcessStartInfo(python, @"-EsB -X utf8 scripts\setup_desktop.py --ensure")
            {
                WorkingDirectory = folder,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            info.EnvironmentVariables.Remove("PYTHONHOME");
            info.EnvironmentVariables.Remove("PYTHONPATH");
            info.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
            using (var log = new StreamWriter(logPath))
            using (var process = new Process { StartInfo = info })
            {
                DataReceivedEventHandler write = delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data != null) lock (log) { log.WriteLine(e.Data); log.Flush(); }
                };
                process.OutputDataReceived += write;
                process.ErrorDataReceived += write;
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                if (process.ExitCode != 0)
                    throw new IOException("文件已解压，但 Python 环境安装失败。请查看安装目录的 install.log，处理后双击「安装.cmd」重试。");
            }
        }
    }

    internal sealed class InstallerWindow : Form
    {
        private readonly Release release = Release.Read();
        private readonly TextBox url = new TextBox(), folder = new TextBox();
        private readonly Button browse = new Button { Text = "浏览…" };
        private readonly Button install = new Button { Text = "安装" };
        private readonly Button cancel = new Button { Text = "取消" };
        private readonly Label status = new Label();
        private readonly ProgressBar progress = new ProgressBar();
        private CancellationTokenSource operation;
        private bool configuring;

        public InstallerWindow()
        {
            Text = "Ronova 在线安装";
            Font = SystemFonts.MessageBoxFont;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(560, 248);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            AddLabel("资源下载链接", 18);
            url.SetBounds(18, 42, 524, 26);
            url.Text = release.Url;
            url.ReadOnly = release.Url.Length != 0;
            AddLabel("安装目录", 82);
            folder.SetBounds(18, 106, 432, 26);
            folder.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RonovaApp", release.Folder);
            browse.SetBounds(460, 105, 82, 27);
            browse.Click += delegate
            {
                using (var dialog = new FolderBrowserDialog { Description = "请选择空目录安装 Ronova" })
                    if (dialog.ShowDialog(this) == DialogResult.OK) folder.Text = dialog.SelectedPath;
            };
            status.SetBounds(18, 144, 524, 20);
            status.Text = release.Url.Length == 0 ? "请粘贴此版本 ZIP 的 HTTPS 直链。"
                : String.Format("安装需要联网，下载约 {0:F1} GiB。", release.DownloadSize / 1073741824.0);
            progress.SetBounds(18, 170, 524, 16);
            install.SetBounds(354, 205, 90, 28);
            cancel.SetBounds(452, 205, 90, 28);
            Controls.AddRange(new Control[] { url, folder, browse, status, progress, install, cancel });
            AcceptButton = install;
            install.Click += StartInstall;
            cancel.Click += delegate { if (operation == null) Close(); else operation.Cancel(); };
            FormClosing += delegate(object sender, FormClosingEventArgs e)
            {
                if (operation == null) return;
                e.Cancel = true;
                if (!configuring) operation.Cancel();
            };
        }

        private void AddLabel(string text, int y)
        {
            var label = new Label { Text = text, AutoSize = true, Location = new Point(18, y) };
            Controls.Add(label);
        }

        private async void StartInstall(object sender, EventArgs e)
        {
            if (operation != null) return;
            Uri uri;
            string target;
            try
            {
                uri = Install.DownloadUrl(url.Text.Trim());
                target = Install.Destination(folder.Text);
            }
            catch (Exception error)
            {
                MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            operation = new CancellationTokenSource();
            var token = operation.Token;
            install.Enabled = browse.Enabled = folder.Enabled = url.Enabled = false;
            status.Text = "正在连接下载服务器…";
            try
            {
                await Task.Run(() => Install.Execute(release, uri, target, (phase, done, total) =>
                    BeginInvoke((Action)(() => ShowProgress(phase, done, total))), token));
                progress.Style = ProgressBarStyle.Continuous;
                progress.Value = 100;
                status.Text = "安装完成。";
                if (MessageBox.Show(this, "安装完成。现在启动 Ronova 吗？", Text,
                                    MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                    Process.Start(new ProcessStartInfo(Path.Combine(target, "Start-Client.cmd")) {
                        WorkingDirectory = target, UseShellExecute = true
                    });
            }
            catch (OperationCanceledException)
            {
                status.Text = token.IsCancellationRequested
                    ? "安装已取消。已下载的资源可以继续下载。"
                    : "连接下载服务器超时。请重试。";
            }
            catch (Exception error)
            {
                status.Text = "安装未完成。";
                MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally
            {
                operation.Dispose();
                operation = null;
                configuring = false;
                install.Enabled = browse.Enabled = folder.Enabled = url.Enabled = cancel.Enabled = true;
                progress.Style = ProgressBarStyle.Continuous;
            }
        }

        private void ShowProgress(string phase, long done, long total)
        {
            if (operation == null) return;
            configuring = total == 0;
            cancel.Enabled = !configuring;
            progress.Style = configuring ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
            if (configuring) { status.Text = phase; return; }
            progress.Value = (int)Math.Min(100, 100.0 * done / total);
            status.Text = String.Format("{0}  {1:F2} / {2:F2} GiB", phase,
                done / 1073741824.0, total / 1073741824.0);
        }

        [STAThread]
        private static void Main()
        {
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new InstallerWindow());
        }
    }
}
