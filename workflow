# 2. Copy the service files (use forward slashes for scp cross-platform compatibility)
scp -r services/ai-service/* spider@spider-w.local:/home/spider/ai-service/

# 3. Strip any Windows CRLF line endings, install, and tail logs
ssh -t spider@spider-w.local "cd ~/ai-service && sed -i 's/\r$//' deploy/*.sh && sudo bash ./deploy/install-ai-service.sh && sudo journalctl -u hexapod-ai -f -n 50"