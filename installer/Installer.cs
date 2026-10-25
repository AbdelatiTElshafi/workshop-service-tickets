// install.exe - RT Workshop installer / updater / uninstaller (WinForms, C# 5, built by build.ps1).
// The app files travel inside this exe as the embedded resource "payload.zip".
//   install.exe              -> setup window (new install, or update when already installed)
//   uninstall.exe /uninstall -> remove (the installer copies itself into the install folder as uninstall.exe)
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.NetworkInformation;
using System.Reflection;
using System.ServiceProcess;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

static class Const
{
    public const string AppName = "RT Workshop";
    public const string RegKey = @"SOFTWARE\RTWorkshop";
    public const string UninstallKey = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\RTWorkshop";
    public const string AppService = "RTWorkshop";
    public const string DefaultDir = @"C:\RTWorkshop";
    public const int DefaultPort = 8085;
    public static readonly string[] AppFiles = { "server.py", "setup.ps1", "RTWorkshopService.exe", "README.md", "static" };

    public static RegistryKey Hklm() { return RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64); }

    public static string Installed(string name)
    {
        using (var k = Hklm().OpenSubKey(RegKey))
            return k == null ? null : k.GetValue(name) as string;
    }

    public static MessageBoxOptions Rtl { get { return MessageBoxOptions.RtlReading | MessageBoxOptions.RightAlign; } }

    public static string Shortcut(string folder) { return Path.Combine(folder, AppName + ".url"); }
    public static string MenuDir { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonPrograms), AppName); } }
    public static string DesktopDir { get { return Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory); } }

    // Runs setup.ps1 hidden; onLine gets every output line.
    public static int RunSetup(string dir, string args, Action<string> onLine, Action<ProcessStartInfo> env)
    {
        var psi = new ProcessStartInfo("powershell.exe",
            "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(dir, "setup.ps1") + "\" " + args);
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8;
        psi.StandardErrorEncoding = Encoding.UTF8;
        psi.WorkingDirectory = dir;
        if (env != null) env(psi);
        using (var p = new Process())
        {
            p.StartInfo = psi;
            p.OutputDataReceived += (s, e) => { if (e.Data != null) onLine(e.Data); };
            p.ErrorDataReceived += (s, e) => { if (e.Data != null) onLine(e.Data); };
            p.Start();
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            p.WaitForExit();
            return p.ExitCode;
        }
    }
}

class SetupForm : Form
{
    readonly Font font = new Font("Segoe UI", 10);
    readonly Font bold = new Font("Segoe UI", 10, FontStyle.Bold);
    Panel page1, page2, page3;
    TextBox txtDir, txtCompany, txtName, txtUser, txtPass, txtPass2;
    NumericUpDown numPort;
    CheckBox chkDesktop, chkLan, chkOpen;
    GroupBox grpAdmin;
    Label lblExisting, lblStatus, lblResult, lblResultText;
    ProgressBar bar;
    Button btnMain, btnCancel;
    bool working, done, isUpdate;
    int installedPort;
    StreamWriter log;

    public SetupForm()
    {
        Text = "تثبيت " + Const.AppName;
        ClientSize = new Size(640, 650);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        RightToLeft = RightToLeft.Yes;
        RightToLeftLayout = true;
        BackColor = Color.White;
        Font = font;
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);

        var header = new Panel { Dock = DockStyle.Top, Height = 104, BackColor = Color.FromArgb(10, 10, 10) };
        var logo = new PictureBox { SizeMode = PictureBoxSizeMode.Zoom, Size = new Size(76, 76), Location = new Point(20, 14) };
        var res = Assembly.GetExecutingAssembly().GetManifestResourceStream("logo.jpg");
        if (res != null) logo.Image = Image.FromStream(res);
        header.Controls.Add(logo);
        header.Controls.Add(new Label { Text = "RT — برنامج قسم الصيانة", ForeColor = Color.White, AutoSize = true,
            Font = new Font("Segoe UI", 16, FontStyle.Bold), Location = new Point(110, 22) });
        header.Controls.Add(new Label { Text = "معالج التثبيت", ForeColor = Color.FromArgb(160, 160, 160), AutoSize = true, Location = new Point(113, 60) });

        var footer = new Panel { Dock = DockStyle.Bottom, Height = 62, BackColor = Color.FromArgb(245, 245, 245) };
        btnMain = new Button { Text = "تثبيت", Size = new Size(150, 36), Location = new Point(20, 13), FlatStyle = FlatStyle.Flat,
            BackColor = Color.Black, ForeColor = Color.White, Font = bold };
        btnMain.FlatAppearance.BorderColor = Color.Black;
        btnCancel = new Button { Text = "إلغاء", Size = new Size(120, 36), Location = new Point(180, 13), FlatStyle = FlatStyle.Flat };
        footer.Controls.Add(btnMain);
        footer.Controls.Add(btnCancel);
        btnMain.Click += OnMain;
        btnCancel.Click += (s, e) => Close();

        BuildPage1();
        BuildPage2();
        BuildPage3();
        Controls.Add(page1);
        Controls.Add(page2);
        Controls.Add(page3);
        Controls.Add(footer);
        Controls.Add(header);
        Show(page1);
        RefreshExisting();
        FormClosing += (s, e) =>
        {
            if (working)
            {
                MessageBox.Show(this, "استنى لحد ما التثبيت يخلص.", Const.AppName, MessageBoxButtons.OK, MessageBoxIcon.Information,
                    MessageBoxDefaultButton.Button1, Const.Rtl);
                e.Cancel = true;
            }
        };
    }

    Panel NewPage() { return new Panel { Location = new Point(0, 104), Size = new Size(640, 484), Visible = false }; }

    Label AddLabel(Control parent, string text, int x, int y, int w, int h, bool isBold)
    {
        var l = new Label { Text = text, Location = new Point(x, y), Size = new Size(w, h), Font = isBold ? bold : font };
        parent.Controls.Add(l);
        return l;
    }

    TextBox AddField(Control parent, string label, int x, int y, int w, bool password)
    {
        AddLabel(parent, label, x, y, w, 22, false);
        var t = new TextBox { Location = new Point(x, y + 23), Size = new Size(w, 26), UseSystemPasswordChar = password };
        parent.Controls.Add(t);
        return t;
    }

    void BuildPage1()
    {
        page1 = NewPage();
        AddLabel(page1, "البرنامج هيتثبت على الجهاز ده ويشتغل لوحده مع الويندوز. أول مرة محتاج نت، وممكن ياخد من 5 لـ 15 دقيقة.", 25, 14, 590, 44, false);

        AddLabel(page1, "مكان التثبيت", 25, 62, 400, 22, false);
        txtDir = new TextBox { Location = new Point(125, 85), Size = new Size(490, 26), RightToLeft = RightToLeft.No,
            Text = Const.Installed("InstallDir") ?? Const.DefaultDir };
        var browse = new Button { Text = "تغيير...", Location = new Point(25, 84), Size = new Size(90, 28) };
        browse.Click += (s, e) =>
        {
            using (var d = new FolderBrowserDialog { SelectedPath = txtDir.Text, Description = "اختار مكان التثبيت" })
                if (d.ShowDialog(this) == DialogResult.OK) txtDir.Text = Path.Combine(d.SelectedPath, "RTWorkshop");
        };
        txtDir.TextChanged += (s, e) => RefreshExisting();
        page1.Controls.Add(txtDir);
        page1.Controls.Add(browse);

        AddLabel(page1, "البورت (رقم الصفحة في المتصفح)", 25, 122, 280, 22, false);
        numPort = new NumericUpDown { Location = new Point(25, 145), Size = new Size(140, 26), Minimum = 1024, Maximum = 65535,
            Value = Const.DefaultPort, RightToLeft = RightToLeft.No };
        page1.Controls.Add(numPort);
        txtCompany = AddField(page1, "اسم الشركة / القسم (بيظهر في الإيصالات)", 315, 122, 300, false);
        txtCompany.Text = "RT — قسم الصيانة";

        grpAdmin = new GroupBox { Text = "حساب مدير النظام", Location = new Point(25, 186), Size = new Size(590, 168), Font = bold };
        txtName = AddField(grpAdmin, "الاسم بالكامل", 15, 26, 270, false);
        txtUser = AddField(grpAdmin, "اسم الدخول (حروف إنجليزي أو أرقام)", 305, 26, 270, false);
        txtUser.RightToLeft = RightToLeft.No;
        txtPass = AddField(grpAdmin, "كلمة المرور (6 حروف على الأقل)", 15, 90, 270, true);
        txtPass2 = AddField(grpAdmin, "اكتب كلمة المرور تاني", 305, 90, 270, true);
        foreach (Control c in grpAdmin.Controls) c.Font = font;
        page1.Controls.Add(grpAdmin);
        lblExisting = AddLabel(page1, "", 25, 186, 590, 60, false);
        lblExisting.ForeColor = Color.FromArgb(21, 94, 117);
        lblExisting.Visible = false;

        chkDesktop = new CheckBox { Text = "اختصار على سطح المكتب", Checked = true, Location = new Point(25, 368), Size = new Size(590, 26) };
        chkLan = new CheckBox { Text = "اسمح للأجهزة التانية على شبكة الورشة إنها تفتح البرنامج", Location = new Point(25, 396), Size = new Size(590, 26) };
        chkOpen = new CheckBox { Text = "افتح البرنامج بعد التثبيت", Checked = true, Location = new Point(25, 424), Size = new Size(590, 26) };
        page1.Controls.Add(chkDesktop);
        page1.Controls.Add(chkLan);
        page1.Controls.Add(chkOpen);
    }

    void BuildPage2()
    {
        page2 = NewPage();
        AddLabel(page2, "بنثبّت البرنامج... ما تقفلش الجهاز.", 25, 40, 590, 26, true);
        lblStatus = AddLabel(page2, "بجهّز...", 25, 90, 590, 26, false);
        bar = new ProgressBar { Location = new Point(25, 125), Size = new Size(590, 26), Maximum = 100, RightToLeftLayout = true };
        page2.Controls.Add(bar);
    }

    void BuildPage3()
    {
        page3 = NewPage();
        lblResult = AddLabel(page3, "", 25, 40, 590, 36, true);
        lblResult.Font = new Font("Segoe UI", 15, FontStyle.Bold);
        lblResultText = AddLabel(page3, "", 25, 90, 590, 300, false);
    }

    void Show(Panel p)
    {
        page1.Visible = p == page1;
        page2.Visible = p == page2;
        page3.Visible = p == page3;
    }

    // An existing install keeps its data and accounts, so no admin fields then.
    void RefreshExisting()
    {
        string dir = txtDir.Text.Trim();
        isUpdate = dir.Length > 3 && Directory.Exists(Path.Combine(dir, "data", "pgdata"));
        installedPort = 0;
        if (isUpdate)
        {
            try
            {
                var m = Regex.Match(File.ReadAllText(Path.Combine(dir, "data", "config.json")), "\"app_port\"\\s*:\\s*(\\d+)");
                if (m.Success) { installedPort = int.Parse(m.Groups[1].Value); numPort.Value = installedPort; }
                var h = Regex.Match(File.ReadAllText(Path.Combine(dir, "data", "config.json")), "\"app_host\"\\s*:\\s*\"0\\.0\\.0\\.0\"");
                chkLan.Checked = h.Success;
            }
            catch { }
        }
        grpAdmin.Visible = !isUpdate;
        txtCompany.Enabled = !isUpdate;
        lblExisting.Visible = isUpdate;
        lblExisting.Text = "البرنامج متثبّت قبل كده في المكان ده. هيتحدّث، والبيانات والحسابات هتفضل زي ما هي.";
        btnMain.Text = isUpdate ? "تحديث" : "تثبيت";
        Text = (isUpdate ? "تحديث " : "تثبيت ") + Const.AppName;
    }

    void Warn(string msg, Control focus)
    {
        MessageBox.Show(this, msg, Const.AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button1, Const.Rtl);
        if (focus != null) focus.Focus();
    }

    bool Validate1()
    {
        string dir = txtDir.Text.Trim();
        if (!Path.IsPathRooted(dir) || dir.Length < 4) { Warn("اختار مكان تثبيت صحيح، مثلاً C:\\RTWorkshop", txtDir); return false; }
        int port = (int)numPort.Value;
        bool ours = isUpdate && port == installedPort;
        if (!ours && IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners().Any(ep => ep.Port == port))
        {
            Warn("البورت " + port + " مستخدم في برنامج تاني على الجهاز. اختار رقم تاني (مثلاً 8090).", numPort);
            return false;
        }
        if (isUpdate) return true;
        if (txtName.Text.Trim().Length < 2) { Warn("اكتب اسم مدير النظام.", txtName); return false; }
        if (!Regex.IsMatch(txtUser.Text.Trim(), "^[A-Za-z0-9._-]{3,50}$"))
        {
            Warn("اسم الدخول لازم يكون 3 حروف على الأقل، بالإنجليزي أو أرقام، من غير مسافات.", txtUser);
            return false;
        }
        if (txtPass.Text.Length < 6) { Warn("كلمة المرور لازم 6 حروف على الأقل.", txtPass); return false; }
        if (txtPass.Text != txtPass2.Text) { Warn("كلمتين المرور مش زي بعض.", txtPass2); return false; }
        return true;
    }

    void OnMain(object sender, EventArgs e)
    {
        if (done)
        {
            if (chkOpen.Checked) Process.Start("http://localhost:" + numPort.Value + "/");
            Close();
            return;
        }
        if (!Validate1()) return;
        working = true;
        btnMain.Visible = false;
        btnCancel.Visible = false;
        Show(page2);
        var t = new Thread(Work);
        t.IsBackground = true;
        t.Start();
    }

    void Report(int pct, string msg)
    {
        BeginInvoke((Action)(() =>
        {
            if (pct >= 0) bar.Value = Math.Max(bar.Value, Math.Min(100, pct));
            if (msg != null) lblStatus.Text = msg;
        }));
    }

    void Log(string line)
    {
        try { if (log != null) { log.WriteLine(DateTime.Now.ToString("HH:mm:ss ") + line); log.Flush(); } } catch { }
    }

    void Work()
    {
        string dir = txtDir.Text.Trim();
        int port = (int)numPort.Value;
        string error = null;
        try
        {
            Directory.CreateDirectory(Path.Combine(dir, "data", "logs"));
            log = new StreamWriter(Path.Combine(dir, "data", "logs", "install.log"), true, Encoding.UTF8);
            Log("---- " + (isUpdate ? "update" : "install") + " to " + dir + " port " + port);

            Report(2, "بجهّز...");
            StopAppService();

            Report(5, "بنسخ ملفات البرنامج...");
            ExtractPayload(dir);

            string args = "-Mode install -AppPort " + port + (chkLan.Checked ? " -AllowLan" : "");
            string setupError = null;
            int code = Const.RunSetup(dir, args, line =>
            {
                Log(line);
                var m = Regex.Match(line, @"^##(\d+)\|(.*)$");
                if (m.Success) { Report(8 + int.Parse(m.Groups[1].Value) * 88 / 100, m.Groups[2].Value); return; }
                m = Regex.Match(line, @"^##ERR\|(.*)$");
                if (m.Success) setupError = m.Groups[1].Value;
            }, psi =>
            {
                if (isUpdate) return;
                psi.EnvironmentVariables["RT_ADMIN_USER"] = txtUser.Text.Trim();
                psi.EnvironmentVariables["RT_ADMIN_NAME"] = txtName.Text.Trim();
                psi.EnvironmentVariables["RT_ADMIN_PASS"] = txtPass.Text;
                psi.EnvironmentVariables["RT_COMPANY"] = txtCompany.Text.Trim();
            });
            if (code != 0) throw new Exception(setupError ?? ("كود " + code));

            Report(97, "بعمل الاختصارات...");
            WriteShortcuts(dir, port);
            RegisterInstall(dir, port);
            Report(100, "خلاص!");
        }
        catch (Exception ex)
        {
            error = ex.Message;
            Log("ERROR " + ex);
        }
        finally
        {
            if (log != null) log.Dispose();
        }
        BeginInvoke((Action)(() => Finish(error, dir, port)));
    }

    void StopAppService()
    {
        try
        {
            using (var sc = new ServiceController(Const.AppService))
            {
                if (sc.Status != ServiceControllerStatus.Stopped)
                {
                    sc.Stop();
                    sc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
                }
            }
        }
        catch (InvalidOperationException) { }   // not installed yet
    }

    void ExtractPayload(string dir)
    {
        using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (var zip = new ZipArchive(s, ZipArchiveMode.Read))
        {
            foreach (var entry in zip.Entries)
            {
                if (entry.FullName.EndsWith("/")) continue;
                string dest = Path.GetFullPath(Path.Combine(dir, entry.FullName));
                if (!dest.StartsWith(Path.GetFullPath(dir), StringComparison.OrdinalIgnoreCase)) continue;
                Directory.CreateDirectory(Path.GetDirectoryName(dest));
                entry.ExtractToFile(dest, true);
            }
        }
        string self = Application.ExecutablePath, uninst = Path.Combine(dir, "uninstall.exe");
        if (!string.Equals(Path.GetFullPath(self), Path.GetFullPath(uninst), StringComparison.OrdinalIgnoreCase))
            File.Copy(self, uninst, true);
    }

    void WriteShortcuts(string dir, int port)
    {
        string body = "[InternetShortcut]\r\nURL=http://localhost:" + port + "/\r\nIconFile=" + Path.Combine(dir, "static", "rt.ico") + "\r\nIconIndex=0\r\n";
        Directory.CreateDirectory(Const.MenuDir);
        File.WriteAllText(Const.Shortcut(Const.MenuDir), body);
        string desk = Const.Shortcut(Const.DesktopDir);
        if (chkDesktop.Checked) File.WriteAllText(desk, body);
        else if (File.Exists(desk)) File.Delete(desk);
    }

    void RegisterInstall(string dir, int port)
    {
        using (var k = Const.Hklm().CreateSubKey(Const.RegKey))
        {
            k.SetValue("InstallDir", dir);
            k.SetValue("Port", port.ToString());
        }
        using (var k = Const.Hklm().CreateSubKey(Const.UninstallKey))
        {
            k.SetValue("DisplayName", Const.AppName);
            k.SetValue("DisplayIcon", Path.Combine(dir, "static", "rt.ico"));
            k.SetValue("Publisher", "RT");
            k.SetValue("InstallLocation", dir);
            k.SetValue("UninstallString", "\"" + Path.Combine(dir, "uninstall.exe") + "\" /uninstall");
            k.SetValue("NoModify", 1, RegistryValueKind.DWord);
            k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
        }
    }

    void Finish(string error, string dir, int port)
    {
        working = false;
        Show(page3);
        btnMain.Visible = true;
        if (error == null)
        {
            done = true;
            lblResult.Text = isUpdate ? "التحديث خلص ✓" : "التثبيت خلص ✓";
            lblResult.ForeColor = Color.FromArgb(21, 128, 61);
            lblResultText.Text = "البرنامج شغال دلوقتي، وهيشتغل لوحده كل ما الجهاز يفتح.\r\n\r\n" +
                "افتحه من اختصار \"" + Const.AppName + "\" أو من المتصفح على:\r\nhttp://localhost:" + port + "/\r\n\r\n" +
                (isUpdate ? "" : "ادخل باسم الدخول وكلمة المرور اللي كتبتهم.\r\n\r\n") +
                (chkLan.Checked ? "من الأجهزة التانية على الشبكة: http://<IP الجهاز ده>:" + port + "/\r\n\r\n" : "") +
                "النسخ الاحتياطي بيتعمل لوحده كل يوم في:\r\n" + Path.Combine(dir, "data", "backups");
            btnMain.Text = chkOpen.Checked ? "افتح البرنامج" : "إنهاء";
        }
        else
        {
            lblResult.Text = "التثبيت ماكملش ✗";
            lblResult.ForeColor = Color.FromArgb(185, 28, 28);
            lblResultText.Text = error + "\r\n\r\nاتأكد إن النت شغال ودوس \"حاول تاني\". لو المشكلة فضلت، ابعت الملف ده:\r\n" +
                Path.Combine(dir, "data", "logs", "install.log");
            btnMain.Text = "حاول تاني";
            btnCancel.Visible = true;
            btnCancel.Text = "قفل";
        }
    }
}

static class Uninstaller
{
    public static void Run()
    {
        string dir = Const.Installed("InstallDir") ?? AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        if (MessageBox.Show("متأكد إنك عايز تشيل " + Const.AppName + " من الجهاز ده؟", Const.AppName,
                MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2, Const.Rtl) != DialogResult.Yes) return;
        bool keepData = MessageBox.Show("تحتفظ بالبيانات (التذاكر والعملاء والصور والنسخ الاحتياطية)؟\r\n\r\n" +
                "\"نعم\" = البيانات تفضل في فولدر data، ولو ثبّت البرنامج تاني في نفس المكان هترجع.\r\n\"لا\" = البيانات تتمسح نهائي.",
                Const.AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button1, Const.Rtl) == DialogResult.Yes;

        Cursor.Current = Cursors.WaitCursor;
        try { Const.RunSetup(dir, "-Mode uninstall", line => { }, null); } catch { }
        foreach (var f in new[] { Const.Shortcut(Const.DesktopDir), Const.Shortcut(Const.MenuDir) })
            try { if (File.Exists(f)) File.Delete(f); } catch { }
        try { if (Directory.Exists(Const.MenuDir)) Directory.Delete(Const.MenuDir, true); } catch { }
        try { Const.Hklm().DeleteSubKeyTree(Const.UninstallKey, false); } catch { }
        try { Const.Hklm().DeleteSubKeyTree(Const.RegKey, false); } catch { }

        foreach (var name in Const.AppFiles.Concat(new[] { "tools" }).Concat(keepData ? new string[0] : new[] { "data" }))
        {
            string p = Path.Combine(dir, name);
            try
            {
                if (Directory.Exists(p)) Directory.Delete(p, true);
                else if (File.Exists(p)) File.Delete(p);
            }
            catch { }
        }
        MessageBox.Show(Const.AppName + " اتشال من الجهاز." + (keepData ? "\r\n\r\nالبيانات لسه موجودة في:\r\n" + Path.Combine(dir, "data") : ""),
            Const.AppName, MessageBoxButtons.OK, MessageBoxIcon.Information, MessageBoxDefaultButton.Button1, Const.Rtl);
        // uninstall.exe can't delete itself while running: let cmd do it a moment later
        Process.Start(new ProcessStartInfo("cmd.exe", "/c ping 127.0.0.1 -n 3 >nul & del /f /q \"" + Path.Combine(dir, "uninstall.exe") +
            "\" & rmdir \"" + dir + "\"") { CreateNoWindow = true, UseShellExecute = false });
    }
}

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        if (args.Any(a => a.Equals("/uninstall", StringComparison.OrdinalIgnoreCase))) Uninstaller.Run();
        else Application.Run(new SetupForm());
    }
}
