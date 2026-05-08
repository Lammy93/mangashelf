#!/usr/bin/env python3
import os
import pwd
import grp
import sys
import subprocess

os.makedirs("/data/avatars", exist_ok=True)
os.makedirs("/data/cache", exist_ok=True)

if os.geteuid() == 0:
    uid = pwd.getpwnam("mangashelf").pw_uid
    gid = grp.getgrnam("mangashelf").gr_gid
    for vol in ("/data", "/manga"):
        if os.path.exists(vol):
            for root, dirs, files in os.walk(vol):
                for d in dirs:
                    os.chown(os.path.join(root, d), uid, gid)
                for f in files:
                    os.chown(os.path.join(root, f), uid, gid)
            os.chown(vol, uid, gid)
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)

os.execvp(sys.argv[1], sys.argv[1:])
