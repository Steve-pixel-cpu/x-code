// 预加载桥: 给页面提供桌面端能力（UA 不可靠, 普通浏览器没有这些值）
// - xcodeDesktop: 标记桌面端（右键走原生菜单）
// - xcodePickFolder: 弹系统原生"选择文件夹"对话框, 返回绝对路径或 null
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("xcodeDesktop", true);
contextBridge.exposeInMainWorld("xcodePickFolder", async () => {
  return await ipcRenderer.invoke("pick-folder");
});
