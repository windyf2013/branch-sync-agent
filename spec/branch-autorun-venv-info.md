# 分支维护自动化环境信息

## 说明

分支维护自动化通过docker建立容器，在容器内进行进行分支的同步和编译验证工作。
进入容器后，通过代码库固有的编译脚本进行编译。

---

## docker容器启动命令说明

sudo docker run -it \
  --name docker-container-name \
  -v ~/rc_projects/cpe/rcios:/workspace/rcios \
  -v /usr/local:/usr/local \
  rcios-build-env:ubuntu24.04 \
  bash

docker-container-name 格式建议： rcios-sync-{cycle-id}

第一个 -v：映射 RCIOS 代码
格式是：-v 宿主机路径:容器内路径

第二个 -v：映射整个 /usr/local
格式是：-v /usr/local:/usr/local

## 编译脚本说明

代码库根目录下根据产品架构区分：
  1.  RTL9617C **默认**
  2.  RTL9607F
  3.  X86
  4.  EN7529

当前验证仅支持 RTL9617C 产品。 型号分为 5200 **默认**, 2600

脚本路径： build/platform/RTL9617C

脚本名称： RTL9617C_build.sh 通用 **默认** / RTL9617C_build_ct.sh 电信 / RTL9617C_build_cmcc.sh 移动 / RTL9617C_build_cu.sh 联通 / RTL9617C_build_gj.sh 国际

脚本使用方法： RTL9617C_build.sh [product-type] [function-name]  具体方式请自行阅读脚本总结更新

## 编译操作步骤示例

  1. 拉取插件代码

```bash
    cd workspace/rcios/build
    ./code_update.sh -d
```

  2. 清理编译环境残留

```bash
    cd workspace/rcios/build/platform/RTL9617C/
    RTL9617C_build.sh 5200 clean
    rm -rf workspace/rcios/tmp/
```

  3. 完整编译

```bash
    cd workspace/rcios/build/platform/RTL9617C/
    RTL9617C_build.sh 5200 
```

  4. 编译指定功能

```bash
    cd workspace/rcios/build/platform/RTL9617C/
    RTL9617C_build.sh 5200 datapath **编译内核**
    RTL9617C_build.sh 5200 plat dhcp **编译dhcp模块**
    RTL9617C_build.sh 5200 plugin **编译插件**
```
