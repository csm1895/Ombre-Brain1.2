# OmbreBrain V1.2 测试脑启动步骤

只测试，不碰主脑。

## 1. 进项目文件夹
```bash
cd Ombre-Brain1.2-main
```

## 2. 启动测试脑
```bash
docker compose -f docker-compose.test.yml up -d
```

## 3. 看健康状态
```bash
curl http://localhost:18001/health
```

看到 `status: ok` 就算启动成功。

## 4. 浏览器打开前端
```
http://localhost:18001/dashboard
```

## 5. 绝对不要做的事
不要把 `buckets_test` 改成 `/app/buckets`。
不要把主脑目录挂进这个测试容器。
