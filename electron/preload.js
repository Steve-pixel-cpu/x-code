// 预加载桥: 给页面提供桌面端能力（UA 不可靠, 普通浏览器没有这些值）
// - xcodeDesktop: 标记桌面端（右键走应用内自绘菜单）
// - xcodePickFolder: 弹系统原生"选择文件夹"对话框, 返回绝对路径或 null
// - xcodeReadClipboard: 读系统剪贴板文本（右键"粘贴"用——渲染层 execCommand('paste')
//   受浏览器安全模型限制不可用, 必须经主进程 clipboard 模块读取）
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("xcodeDesktop", true);
contextBridge.exposeInMainWorld("xcodePickFolder", async () => {
  return await ipcRenderer.invoke("pick-folder");
});
contextBridge.exposeInMainWorld("xcodeReadClipboard", async () => {
  return await ipcRenderer.invoke("read-clipboard-text");
});
