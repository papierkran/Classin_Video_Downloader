package main

import (
	"context"
	"encoding/csv"
	"fmt"
	"fyne.io/fyne/v2/layout"
	"github.com/chromedp/cdproto/network"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/widget"
	"github.com/chromedp/chromedp"
)

var (
	version = "1.1.0"
)

type AppUI struct {
	app         fyne.App
	window      fyne.Window
	csvEntry    *widget.Entry
	cookieEntry *widget.Entry
	saveEntry   *widget.Entry
	logView     *widget.Entry
	startBtn    *widget.Button
}

func NewAppUI() *AppUI {
	a := app.New()
	w := a.NewWindow("课程视频下载器 v" + version)
	w.Resize(fyne.NewSize(800, 600))

	return &AppUI{
		app:         a,
		window:      w,
		csvEntry:    widget.NewEntry(),
		cookieEntry: widget.NewMultiLineEntry(),
		saveEntry:   widget.NewEntry(),
		logView:     widget.NewMultiLineEntry(),
	}
}

func (ui *AppUI) BuildUI() {
	// 文件选择组件
	fileSelector := container.NewHBox(
		widget.NewLabel("CSV文件路径:"),
		container.New(
			layout.NewGridWrapLayout(fyne.NewSize(400, 40)), // 输入框高度40像素
			container.NewHScroll(ui.csvEntry),
		),
		widget.NewButton("浏览...", ui.selectCSVFile),
	)

	// 保存路径组件
	savePathSelector := container.NewHBox(
		widget.NewLabel("保存目录:"),
		// 创建固定高度的容器包裹滚动视图
		container.New(
			layout.NewGridWrapLayout(fyne.NewSize(400, 40)), // 输入框高度40像素
			container.NewHScroll(ui.saveEntry),
		),
		widget.NewButton("浏览...", ui.selectSaveDir),
	)

	// Cookie输入组件
	cookieBox := container.NewVBox(
		widget.NewLabel("Cookie值:"),
		ui.cookieEntry,
	)

	// 日志组件
	// 1. 创建日志滚动容器
	logScroll := container.NewVScroll(ui.logView)
	// 2. 设置最小高度约束
	logScroll.SetMinSize(fyne.NewSize(0, 200)) // 最小高度200像素
	// 3. 嵌套到弹性布局中
	logBox := container.NewVBox(
		widget.NewLabel("下载日志:"),
		container.New(
			layout.NewVBoxLayout(), // 输入框高度40像素
			container.NewHScroll(ui.logView),
		),
	)

	// 控制按钮
	ui.startBtn = widget.NewButton("开始下载", ui.startDownload)
	ui.startBtn.Importance = widget.HighImportance

	// 主布局
	mainContent := container.NewVBox(
		fileSelector,
		savePathSelector,
		cookieBox,
		ui.startBtn,
		logBox,
	)

	// 支持文件拖放
	ui.window.SetOnDropped(ui.handleFileDrop)
	ui.window.SetContent(mainContent)
}

func (ui *AppUI) selectCSVFile() {
	dialog.ShowFileOpen(func(uc fyne.URIReadCloser, err error) {
		if err == nil && uc != nil {
			ui.csvEntry.SetText(uc.URI().Path())
		}
	}, ui.window)
}

func (ui *AppUI) selectSaveDir() {
	dialog.ShowFolderOpen(func(lu fyne.ListableURI, err error) {
		if err == nil && lu != nil {
			ui.saveEntry.SetText(lu.Path())
		}
	}, ui.window)
}

func (ui *AppUI) handleFileDrop(pos fyne.Position, uris []fyne.URI) {
	if len(uris) > 0 && strings.HasSuffix(uris[0].Path(), ".csv") {
		ui.csvEntry.SetText(uris[0].Path())
	}
}

func (ui *AppUI) startDownload() {
	if !ui.validateInputs() {
		return
	}

	ui.startBtn.Disable()
	defer ui.startBtn.Enable()

	go func() {
		ui.appendLog("=== 开始下载任务 ===")
		defer ui.appendLog("=== 任务结束 ===")

		// 配置浏览器选项
		opts := append(chromedp.DefaultExecAllocatorOptions[:],
			chromedp.Flag("headless", false),
			chromedp.Flag("disable-web-security", true),
			chromedp.UserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"),
		)

		allocCtx, cancel := chromedp.NewExecAllocator(context.Background(), opts...)
		defer cancel()

		ctx, cancel := chromedp.NewContext(allocCtx,
			chromedp.WithLogf(func(format string, args ...interface{}) {
				ui.appendLog(fmt.Sprintf("[BROWSER] "+format, args...))
			}),
		)
		defer cancel()

		// 设置Cookie
		if err := setCookies(ctx, ui.cookieEntry.Text, ".eeo.cn"); err != nil {
			ui.appendLog("设置Cookie失败: " + err.Error())
			return
		}

		// 加载课程数据
		courses, err := loadCoursesFromCSV(ui.csvEntry.Text)
		if err != nil {
			ui.appendLog("加载CSV失败: " + err.Error())
			return
		}

		// 创建保存目录
		saveDir := ui.saveEntry.Text
		if err := os.MkdirAll(saveDir, 0755); err != nil {
			ui.appendLog("创建目录失败: " + err.Error())
			return
		}

		// 并发控制
		sem := make(chan struct{}, 5)
		defer close(sem)

		for i, course := range courses {
			select {
			case sem <- struct{}{}:
				go func(idx int, c map[string]string) {
					defer func() { <-sem }()
					ui.processCourse(ctx, c, idx+1, len(courses), saveDir)
				}(i, course)
			case <-time.After(30 * time.Second):
				ui.appendLog("操作超时，请检查网络连接")
				return
			}
		}
	}()
}

func (ui *AppUI) processCourse(ctx context.Context, course map[string]string, index, total int, saveDir string) {
	courseID := course["课程ID"]
	lessonID := course["课节ID"]
	progress := fmt.Sprintf("[%d/%d]", index, total)

	ui.appendLog(fmt.Sprintf("\n%s 处理课程: %s", progress, courseID))

	courseName, videos, err := getDownloadInfo(ctx, courseID, lessonID)
	if err != nil {
		ui.appendLog(fmt.Sprintf("获取课程信息失败: %v", err))
		return
	}

	for _, video := range videos {
		ui.downloadVideo(video, courseName, saveDir)
		time.Sleep(1 * time.Second)
	}
}

func (ui *AppUI) downloadVideo(video map[string]string, courseName, saveDir string) {
	filename := generateFilename(video, courseName)
	savePath := filepath.Join(saveDir, filename)

	ui.appendLog("正在下载: " + filename)

	if err := downloadFile(
		video["download_url"],
		savePath,
		parseCookieString(ui.cookieEntry.Text),
	); err != nil {
		ui.appendLog(fmt.Sprintf("下载失败: %s - %v", filename, err))
	} else {
		ui.appendLog("下载成功: " + filename)
	}
}

func (ui *AppUI) validateInputs() bool {
	if ui.csvEntry.Text == "" {
		ui.showError("请选择CSV文件")
		return false
	}

	if ui.cookieEntry.Text == "" {
		ui.showError("请输入Cookie")
		return false
	}

	if ui.saveEntry.Text == "" {
		ui.showError("请选择保存目录")
		return false
	}

	return true
}

func (ui *AppUI) showError(msg string) {
	dialog.ShowInformation("错误", msg, ui.window)
}

func (ui *AppUI) appendLog(text string) {
	ui.app.SendNotification(fyne.NewNotification("日志更新", text))
	ui.logView.SetText(ui.logView.Text + text + "\n")
	ui.logView.CursorRow = len(strings.Split(ui.logView.Text, "\n")) - 1
}

// 核心功能函数
// 设置 Cookie 的修正版本
func setCookies(ctx context.Context, cookieStr string, domain string) error {
	// 解析 cookie 字符串
	cookies := parseCookieString(cookieStr)

	// 转换为 chromedp 所需的 network.Cookie 类型
	var cdpCookies []*network.Cookie
	for _, c := range cookies {
		cdpCookies = append(cdpCookies, &network.Cookie{
			Name:   c.Name,
			Value:  c.Value,
			Domain: domain,
			Path:   "/",
			Secure: c.Secure,
		})
	}

	// 设置 cookies
	return chromedp.Run(
		ctx,
		chromedp.Navigate("https://www.eeo.cn"), // 使用转换后的 cookies
	)
}

func generateFilename(video map[string]string, courseName string) string {
	base := fmt.Sprintf("%s_%s_%s",
		video["record_date"],
		sanitizeFilename(courseName),
		sanitizeFilename(video["segment_title"]),
	)
	return generateUniqueFilename(base, ".mp4", "")
}

func sanitizeFilename(name string) string {
	return regexp.MustCompile(`[<>:"/\\|?*]`).ReplaceAllString(name, "_")
}

func generateUniqueFilename(base, ext, dir string) string {
	counter := 0
	for {
		suffix := ""
		if counter > 0 {
			suffix = fmt.Sprintf("(%d)", counter)
		}
		filename := fmt.Sprintf("%s%s%s", base, suffix, ext)
		fullpath := filepath.Join(dir, filename)
		if _, err := os.Stat(fullpath); os.IsNotExist(err) {
			return filename
		}
		counter++
	}
}

func downloadFile(url, path string, cookies []*http.Cookie) error {
	client := &http.Client{Timeout: 30 * time.Minute}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return err
	}

	for _, c := range cookies {
		req.AddCookie(c)
	}

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("请求失败: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("服务器返回错误状态码: %d", resp.StatusCode)
	}

	file, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("创建文件失败: %v", err)
	}
	defer file.Close()

	_, err = io.Copy(file, resp.Body)
	if err != nil {
		return fmt.Errorf("写入文件失败: %v", err)
	}

	return nil
}

func parseCookieString(cookieStr string) []*http.Cookie {
	var cookies []*http.Cookie
	for _, pair := range strings.Split(cookieStr, ";") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		parts := strings.SplitN(pair, "=", 2)
		if len(parts) == 2 {
			cookies = append(cookies, &http.Cookie{
				Name:  parts[0],
				Value: parts[1],
			})
		}
	}
	return cookies
}

func loadCoursesFromCSV(path string) ([]map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("打开文件失败: %v", err)
	}
	defer file.Close()

	reader := csv.NewReader(file)
	reader.FieldsPerRecord = -1 // 允许可变列数

	// 跳过前3行
	for i := 0; i < 3; i++ {
		if _, err := reader.Read(); err != nil {
			return nil, fmt.Errorf("跳过标题行失败: %v", err)
		}
	}

	headers, err := reader.Read()
	if err != nil {
		return nil, fmt.Errorf("读取列头失败: %v", err)
	}

	var courses []map[string]string
	lineNum := 4 // 前3行已跳过
	for {
		record, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("行%d读取失败: %v", lineNum, err)
		}

		if len(record) != len(headers) {
			return nil, fmt.Errorf("行%d列数不匹配 (预期%d列，实际%d列)",
				lineNum, len(headers), len(record))
		}

		course := make(map[string]string)
		for i, val := range record {
			course[headers[i]] = strings.TrimSpace(val)
		}
		courses = append(courses, course)
		lineNum++
	}

	if len(courses) == 0 {
		return nil, fmt.Errorf("未找到有效课程数据")
	}

	return courses, nil
}

func getDownloadInfo(ctx context.Context, courseID, lessonID string) (string, []map[string]string, error) {
	url := fmt.Sprintf(
		"https://console.eeo.cn/saas/school/index.html#/singlePage/CourseManagement/recordLessonManagement?courseId=%s&lessonId=%s&record=true&live=true",
		courseID, lessonID,
	)

	var courseName string
	var videos []map[string]string

	err := chromedp.Run(ctx,
		chromedp.Navigate(url),
		chromedp.WaitReady(`table > tbody > tr:first-child`, chromedp.ByQuery),
		chromedp.Sleep(2*time.Second),
		chromedp.Text(`//p[contains(.,'课程名称：')]/span`, &courseName, chromedp.NodeVisible),
		chromedp.Evaluate(`
			Array.from(document.querySelectorAll('table tbody tr')).map(row => {
				const cells = row.querySelectorAll('td');
				return {
					download_url: cells[8]?.querySelector('a')?.href || '',
					record_date: cells[4]?.textContent?.trim() || '',
					segment_title: cells[1]?.textContent?.trim() || '',
					record_method: cells[3]?.textContent?.trim() || ''
				};
			})`, &videos),
	)

	if err != nil {
		return "", nil, fmt.Errorf("页面操作失败: %v", err)
	}

	// 清理数据
	courseName = strings.TrimPrefix(courseName, "课程名称：")
	courseName = sanitizeFilename(courseName)

	// 过滤无效条目
	validVideos := make([]map[string]string, 0)
	for _, v := range videos {
		if v["download_url"] != "" && strings.HasSuffix(v["download_url"], ".mp4") {
			v["record_date"] = formatDate(v["record_date"])
			validVideos = append(validVideos, v)
		}
	}

	return courseName, validVideos, nil
}

func formatDate(dateStr string) string {
	t, err := time.Parse("2006-01-02 15:04:05", dateStr)
	if err != nil {
		return strings.Split(dateStr, " ")[0]
	}
	return t.Format("2006-01-02")
}

func main() {
	ui := NewAppUI()
	ui.BuildUI()
	ui.window.ShowAndRun()
}
