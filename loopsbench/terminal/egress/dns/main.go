// The task can read only a trusted snapshot of authorized A/AAAA records.
// No task-supplied DNS name or packet is ever forwarded to an upstream.
package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"syscall"
	"time"

	"golang.org/x/net/dns/dnsmessage"
)

const dnsMark = 114515

type records map[string][]net.IP

func snapshot(allowlist, output string) error {
	body, err := os.ReadFile(allowlist)
	if err != nil {
		return err
	}
	resolv, err := os.ReadFile("/tmp/egress-resolv.conf")
	if err != nil {
		return err
	}
	var servers []string
	for _, line := range strings.Split(string(resolv), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[0] == "nameserver" && net.ParseIP(fields[1]) != nil {
			servers = append(servers, net.JoinHostPort(fields[1], "53"))
		}
	}
	if len(servers) == 0 {
		return fmt.Errorf("no trusted bootstrap nameserver")
	}
	hosts, err := os.ReadFile("/tmp/egress-hosts")
	if err != nil {
		return err
	}
	static := records{}
	for _, line := range strings.Split(string(hosts), "\n") {
		fields := strings.Fields(strings.SplitN(line, "#", 2)[0])
		if len(fields) < 2 {
			continue
		}
		if ip := net.ParseIP(fields[0]); ip != nil {
			for _, name := range fields[1:] {
				static[strings.ToLower(strings.TrimSuffix(name, "."))] = append(static[strings.ToLower(strings.TrimSuffix(name, "."))], ip)
			}
		}
	}
	resolver := &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		dialer := net.Dialer{Timeout: 5 * time.Second, Control: func(_, _ string, raw syscall.RawConn) error {
			var markErr error
			err := raw.Control(func(fd uintptr) {
				markErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_MARK, dnsMark)
			})
			if err != nil {
				return err
			}
			return markErr
		}}
		// /etc/resolv.conf is task-writable. Only bootstrap nameservers may
		// receive marked traffic, irrespective of that file's current content.
		trusted := servers[0]
		for _, server := range servers {
			if server == address {
				trusted = server
				break
			}
		}
		return dialer.DialContext(ctx, network, trusted)
	}}
	result := records{}
	for _, name := range strings.Fields(string(body)) {
		if net.ParseIP(name) != nil {
			continue
		}
		if _, _, err := net.ParseCIDR(name); err == nil {
			continue
		}
		if strings.ContainsAny(name, "*/") {
			return fmt.Errorf("DNS requires exact authorized hostnames")
		}
		name = strings.ToLower(strings.TrimSuffix(name, "."))
		if addresses := static[name]; len(addresses) > 0 {
			result[name+"."] = addresses
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		addresses, err := resolver.LookupIPAddr(ctx, name+".")
		cancel()
		if err != nil {
			return fmt.Errorf("resolve authorized host %s: %w", name, err)
		}
		if len(addresses) == 0 {
			return fmt.Errorf("authorized host %s has no addresses", name)
		}
		for _, address := range addresses {
			result[name+"."] = append(result[name+"."], address.IP)
		}
	}
	body, err = json.Marshal(result)
	if err != nil {
		return err
	}
	if err = os.WriteFile(output+".tmp", body, 0600); err != nil {
		return err
	}
	return os.Rename(output+".tmp", output)
}

func answer(packet []byte, allowed records) []byte {
	var parser dnsmessage.Parser
	header, err := parser.Start(packet)
	if err != nil {
		return nil
	}
	question, err := parser.Question()
	if err != nil {
		return nil
	}
	response := dnsmessage.Header{ID: header.ID, Response: true, RecursionDesired: header.RecursionDesired}
	_, next := parser.Question()
	if header.Response || header.OpCode != 0 || next != dnsmessage.ErrSectionDone {
		response.RCode = dnsmessage.RCodeFormatError
	} else if question.Class != dnsmessage.ClassINET || (question.Type != dnsmessage.TypeA && question.Type != dnsmessage.TypeAAAA) {
		response.RCode = dnsmessage.RCodeRefused
	} else if _, ok := allowed[strings.ToLower(question.Name.String())]; !ok {
		response.RCode = dnsmessage.RCodeRefused
	}
	builder := dnsmessage.NewBuilder(nil, response)
	builder.EnableCompression()
	if builder.StartQuestions() != nil || builder.Question(question) != nil {
		return nil
	}
	if response.RCode == dnsmessage.RCodeSuccess {
		if builder.StartAnswers() != nil {
			return nil
		}
		for _, address := range allowed[strings.ToLower(question.Name.String())] {
			rh := dnsmessage.ResourceHeader{Name: question.Name, Class: dnsmessage.ClassINET, TTL: 0}
			if question.Type == dnsmessage.TypeA {
				if ip := address.To4(); ip != nil {
					var a [4]byte
					copy(a[:], ip)
					if builder.AResource(rh, dnsmessage.AResource{A: a}) != nil {
						return nil
					}
				}
			} else if address.To4() == nil {
				if ip := address.To16(); ip != nil {
					var a [16]byte
					copy(a[:], ip)
					if builder.AAAAResource(rh, dnsmessage.AAAAResource{AAAA: a}) != nil {
						return nil
					}
				}
			}
		}
	}
	result, err := builder.Finish()
	if err != nil {
		return nil
	}
	return result
}

func serve(path string) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	allowed := records{}
	if err = json.Unmarshal(body, &allowed); err != nil {
		return err
	}
	udp, err := net.ListenPacket("udp", ":1053")
	if err != nil {
		return err
	}
	defer udp.Close()
	tcp, err := net.Listen("tcp", ":1053")
	if err != nil {
		return err
	}
	defer tcp.Close()
	go func() {
		buffer := make([]byte, 4096)
		for {
			n, peer, err := udp.ReadFrom(buffer)
			if err != nil {
				return
			}
			if response := answer(buffer[:n], allowed); len(response) > 0 {
				_, _ = udp.WriteTo(response, peer)
			}
		}
	}()
	if err := os.WriteFile("/tmp/dns.ready", []byte("ready\n"), 0600); err != nil {
		return err
	}
	slots := make(chan struct{}, 32)
	for {
		conn, err := tcp.Accept()
		if err != nil {
			return err
		}
		select {
		case slots <- struct{}{}:
			go func() {
				defer func() { conn.Close(); <-slots }()
				for {
					_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
					var length [2]byte
					if _, err := io.ReadFull(conn, length[:]); err != nil {
						return
					}
					n := int(binary.BigEndian.Uint16(length[:]))
					if n < 12 || n > 4096 {
						return
					}
					packet := make([]byte, n)
					if _, err := io.ReadFull(conn, packet); err != nil {
						return
					}
					response := answer(packet, allowed)
					if len(response) == 0 {
						return
					}
					binary.BigEndian.PutUint16(length[:], uint16(len(response)))
					if _, err := conn.Write(append(length[:], response...)); err != nil {
						return
					}
				}
			}()
		default:
			conn.Close()
		}
	}
}

func main() {
	var err error
	switch {
	case len(os.Args) == 4 && os.Args[1] == "snapshot":
		err = snapshot(os.Args[2], os.Args[3])
	case len(os.Args) == 3 && os.Args[1] == "serve":
		err = serve(os.Args[2])
	default:
		err = fmt.Errorf("usage: egress-dns snapshot ALLOWLIST OUTPUT | serve RECORDS")
	}
	if err != nil {
		log.Fatal(err)
	}
}
