# RCIOS 编译日志示例

## 开始编译输出示例

```bash
root@c486a8577d28:/workspace/rcios/build/platform/RTL9617C# ./RTL9617C_build.sh 5200
Auto Generate rcios config...
Patching kernel src...
Make uImage...
make[1]: Entering directory '/workspace/rcios/tmp/RTL9617C/kernel'
  GEN     Makefile
  CALL    /workspace/rcios/datapath/kernel/linux-rtk/linux-5.10.70/scripts/checksyscalls.sh
  CALL    /workspace/rcios/datapath/kernel/linux-rtk/linux-5.10.70/scripts/atomic/check-atomics.sh
  CHK     include/generated/compile.h
fatal: not a git repository: '/workspace/rcios/build/../datapath/kernel/linux-rtk/linux-5.10.70/drivers/net/ethernet/realtek/rtl86900/FleetConntrackDriver/.git'
  CC [M]  drivers/net/ethernet/realtek/rtl86900/FleetConntrackDriver/core/rtk_fc_driver.o
```

---

## 编译 plat 下 route 输出示例

```bash
/workspace/rcios/plat/route/bgpd/bgp_table.h:328:31: warning: cast to pointer from integer of different size [-Wint-to-pointer-cast]
  328 |   return bgp_node_from_rnode ((struct bgp_node *)route_table_iter_next (&iter->rt_iter));
      |                               ^
/workspace/rcios/plat/route/bgpd/bgp_table.h: In function 'bgp_table_iter_cleanup':
/workspace/rcios/plat/route/bgpd/bgp_table.h:337:3: warning: implicit declaration of function 'route_table_iter_cleanup'; did you mean 'bgp_table_iter_cleanup'? [-Wimplicit-function-declaration]
  337 |   route_table_iter_cleanup (&iter->rt_iter);
      |   ^~~~~~~~~~~~~~~~~~~~~~~~
      |   bgp_table_iter_cleanup
/workspace/rcios/plat/route/bgpd/bgp_table.h: In function 'bgp_table_iter_pause':
/workspace/rcios/plat/route/bgpd/bgp_table.h:348:3: warning: implicit declaration of function 'route_table_iter_pause'; did you mean 'bgp_table_iter_pause'? [-Wimplicit-function-declaration]
  348 |   route_table_iter_pause (&iter->rt_iter);
      |   ^~~~~~~~~~~~~~~~~~~~~~
      |   bgp_table_iter_pause
/workspace/rcios/plat/route/bgpd/bgp_table.h: In function 'bgp_table_iter_is_done':
/workspace/rcios/plat/route/bgpd/bgp_table.h:357:10: warning: implicit declaration of function 'route_table_iter_is_done'; did you mean 'bgp_table_iter_is_done'? [-Wimplicit-function-declaration]
  357 |   return route_table_iter_is_done (&iter->rt_iter);
      |          ^~~~~~~~~~~~~~~~~~~~~~~~
      |          bgp_table_iter_is_done
/workspace/rcios/plat/route/bgpd/bgp_table.h: In function 'bgp_table_iter_started':
/workspace/rcios/plat/route/bgpd/bgp_table.h:366:10: warning: implicit declaration of function 'route_table_iter_started'; did you mean 'bgp_table_iter_started'? [-Wimplicit-function-declaration]
  366 |   return route_table_iter_started (&iter->rt_iter);
```

---

## 编译 component 下 wlan 输出示例

```bash
/workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/platform/platform_linux_pc_pci.c: In function 'pci_use_shared_xo':
/workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/platform/platform_linux_pc_pci.c:111:10: note: '#pragma message: Wlan device sharing other's XO is from default value "0001:01:00.0"'
  111 |  #pragma message("Wlan device sharing other's XO is from default value \"" "0001:01:00.0" "\"")
      |          ^~~~~~~
/workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/platform/platform_linux_pc_pci.c: In function 'dev_get_band_cap':
/workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/platform/platform_linux_pc_pci.c:134:10: note: '#pragma message: 2.4G wlan device is from CONFIG_RTW_2_4G_DEV: "0001:01:00.0"'
  134 |  #pragma message("2.4G wlan device is from CONFIG_RTW_2_4G_DEV: \"" CONFIG_RTW_2_4G_DEV "\"")
      |          ^~~~~~~
  CC [M]  /workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/platform/rtk_ap/rtw_api_flash.o
  CC [M]  /workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/platform/rtk_ap/flash/rtw_flash.o
  CC [M]  /workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/os_dep/osdep_service.o
  CC [M]  /workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/os_dep/osdep_service_linux.o
  CC [M]  /workspace/rcios/component/wlan/realtek/rtk_wifi6/g6_wifi_driver/os_dep/linux/rtw_cfg.o
```

---

## 成功编译完成后输出示例

```bash
Parallel mksquashfs: Using 12 processors
Creating 4.0 filesystem on /workspace/rcios/build/../tmp/RTL9617C/images/squashrootfs, block size 131072.
[===================================================================================================================================================================================================================-] 3417/3417 100%

Exportable Squashfs 4.0 filesystem, xz compressed, data block size 131072
	compressed data, compressed metadata, compressed fragments,
	no xattrs, compressed ids
	duplicates are removed
Filesystem size 29161.63 Kbytes (28.48 Mbytes)
	22.48% of uncompressed filesystem size (129745.86 Kbytes)
Inode table size 23082 bytes (22.54 Kbytes)
	23.18% of uncompressed inode table size (99568 bytes)
Directory table size 24398 bytes (23.83 Kbytes)
	33.70% of uncompressed directory table size (72395 bytes)
Number of duplicate files found 593
Number of inodes 2990
Number of files 2653
Number of fragments 206
Number of symbolic links  56
Number of device nodes 0
Number of fifo nodes 0
Number of socket nodes 0
Number of directories 281
Number of ids (unique uids + gids) 2
Number of uids 2
	root (0)
	ubuntu (1000)
Number of gids 2
	root (0)
	ubuntu (1000)
Make rootfs success
kernel.bin size: 14379016
1+0 records in
1+0 records out
24 bytes copied, 2.4639e-05 s, 974 kB/s
1+0 records in
1+0 records out
2398176 bytes (2.4 MB, 2.3 MiB) copied, 0.00125083 s, 1.9 GB/s
rootfs size: 29863936
convert file rcios_boot_upgrade.bin to MSG5200_BUP_SYSTEM_4.33.204_20260820.bin success!
convert file rcios.bin to MSG5200_DECRYPT_SYSTEM_4.33.204_20260820.bin success!
convert file rcios.bin to MSG5200_SYSTEM_4.33.204_20260820.bin success!
root@c486a8577d28:/workspace/rcios/build/platform/RTL9617C#
```
