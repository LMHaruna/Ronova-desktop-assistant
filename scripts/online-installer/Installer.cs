using System;
using System.Collections.Generic;
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
    internal sealed class Asset
    {
        public readonly Uri Url;
        public readonly long Size;
        public readonly string Hash;

        public Asset(Uri url, long size, string hash)
        {
            Url = url;
            Size = size;
            Hash = hash;
        }
    }

    internal sealed class Release
    {
        public readonly string Folder, Hash, Url;
        public readonly long DownloadSize, InstalledSize;
        public readonly List<Asset> Parts = new List<Asset>();

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
                var release = new Release(reader.ReadLine(), reader.ReadLine(),
                    long.Parse(reader.ReadLine(), CultureInfo.InvariantCulture),
                    long.Parse(reader.ReadLine(), CultureInfo.InvariantCulture), reader.ReadLine() ?? "");
                string line;
                long total = 0;
                while ((line = reader.ReadLine()) != null)
                {
                    var fields = line.Split('\t');
                    long size = long.Parse(fields[1], CultureInfo.InvariantCulture);
                    if (size <= 0 || size >= 2147483648L || fields[0].Length != 64)
                        throw new IOException("安装器的分卷清单无效。");
                    release.Parts.Add(new Asset(Install.DownloadUrl(fields[2]), size, fields[0]));
                    total = checked(total + size);
                }
                if (release.Parts.Count != 0 && total != release.DownloadSize)
                    throw new IOException("安装器的分卷总大小无效。");
                return release;
            }
        }
    }

    // ZipArchive seeks to the central directory before extracting. Present the
    // verified files as a single seekable archive without creating another ZIP.
    internal sealed class MultipartStream : Stream
    {
        private readonly IList<FileStream> parts;
        private readonly long length;
        private long position;

        public MultipartStream(IList<FileStream> parts)
        {
            this.parts = parts;
            foreach (var part in parts) length += part.Length;
        }

        public override bool CanRead { get { return true; } }
        public override bool CanSeek { get { return true; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } }
        public override long Position
        {
            get { return position; }
            set { Seek(value, SeekOrigin.Begin); }
        }

        public override long Seek(long offset, SeekOrigin origin)
        {
            long target = checked(offset + (origin == SeekOrigin.End ? length :
                origin == SeekOrigin.Current ? position : 0));
            if (target < 0) throw new IOException("无效的 ZIP 偏移量。");
            return position = target;
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            int result = 0;
            long start = 0;
            foreach (var part in parts)
            {
                long end = start + part.Length;
                if (position < end && count > 0)
                {
                    part.Position = position - start;
                    int read = part.Read(buffer, offset + result, (int)Math.Min(count, end - position));
                    if (read == 0) throw new EndOfStreamException("下载分卷在解压时被截断。");
                    position += read;
                    result += read;
                    count -= read;
                    if (position < end) break;
                }
                start = end;
            }
            return result;
        }

        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
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

        public static bool Verify(Stream file, long size, string hash, Action<long> update, CancellationToken cancel)
        {
            if (file.Length != size) return false;
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
                bool valid = String.Equals(BitConverter.ToString(sha.Hash).Replace("-", ""), hash,
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
            CancellationToken cancel, HttpMessageHandler transport = null)
        {
            string parent = Path.GetDirectoryName(destination);
            Directory.CreateDirectory(parent);
            string work = Path.Combine(parent, ".ronova-install-" + Guid.NewGuid().ToString("N"));
            var assets = release.Parts.Count == 0
                ? new List<Asset> { new Asset(uri, release.DownloadSize, release.Hash) }
                : release.Parts;
            string prefix = Path.Combine(parent, ".ronova-" + release.Hash.Substring(0, 16));
            var downloads = new List<string>();
            long cached = 0;
            for (int i = 0; i < assets.Count; i++)
            {
                string file = prefix + (release.Parts.Count == 0 ? "" : "-" + (i + 1).ToString("D3")) + ".download-part";
                downloads.Add(file);
                if (File.Exists(file)) cached += Math.Min(new FileInfo(file).Length, assets[i].Size);
            }
            if (new DriveInfo(Path.GetPathRoot(destination)).AvailableFreeSpace <
                release.InstalledSize + release.DownloadSize - cached + 8L * 1024 * 1024 * 1024)
                throw new IOException("磁盘空间不足。请为下载、解压和 Python 环境留出空间。");
            try
            {
                var files = new List<FileStream>();
                try
                {
                    using (var client = new HttpClient(transport ?? new HttpClientHandler { AllowAutoRedirect = false })
                           { Timeout = TimeSpan.FromMinutes(2) })
                    {
                        long completed = 0;
                        for (int i = 0; i < assets.Count; i++)
                        {
                            cancel.ThrowIfCancellationRequested();
                            var asset = assets[i];
                            string download = downloads[i];
                            string label = release.Parts.Count == 0 ? "" : String.Format("（{0}/{1}）", i + 1, assets.Count);
                            await Download(client, asset.Url, download, asset.Size,
                                n => update("正在下载" + label, completed + n, release.DownloadSize), cancel).ConfigureAwait(false);
                            var file = File.OpenRead(download);
                            files.Add(file);
                            update("正在校验下载文件" + label, completed, release.DownloadSize);
                            if (!Verify(file, asset.Size, asset.Hash,
                                n => update("正在校验下载文件" + label, completed + n, release.DownloadSize), cancel))
                            {
                                file.Dispose();
                                File.Delete(download);
                                throw new IOException("下载文件" + label + "的 SHA-256 校验失败。重新点击安装可重下该文件。");
                            }
                            completed += asset.Size;
                        }
                    }
                    using (var archive = new MultipartStream(files))
                    {
                        if (release.Parts.Count != 0 && !Verify(archive, release.DownloadSize, release.Hash,
                                n => update("正在校验完整资源", n, release.DownloadSize), cancel))
                            throw new IOException("全部分卷的合并校验失败。请重新下载指定版本的安装器。");
                        update("正在解压", 0, release.InstalledSize);
                        Extract(archive, work, release, n => update("正在解压", n, release.InstalledSize), cancel);
                    }
                }
                finally { foreach (var file in files) file.Dispose(); }
                foreach (string download in downloads) File.Delete(download);
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
            bool multipart = release.Parts.Count != 0;
            AddLabel(multipart ? "资源来源" : "资源下载链接", 18);
            url.SetBounds(18, 42, 524, 26);
            url.Text = multipart ? "GitHub Releases（自动下载与校验分卷）" : release.Url;
            url.ReadOnly = multipart || release.Url.Length != 0;
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
            status.Text = !multipart && release.Url.Length == 0 ? "请粘贴此版本 ZIP 的 HTTPS 直链。"
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
                uri = release.Parts.Count == 0 ? Install.DownloadUrl(url.Text.Trim()) : null;
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
                {
                    string launcher = Path.Combine(target, "Ronova.exe");
                    if (!File.Exists(launcher)) launcher = Path.Combine(target, "Start-Client.cmd");
                    Process.Start(new ProcessStartInfo(launcher) {
                        WorkingDirectory = target, UseShellExecute = true
                    });
                }
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
