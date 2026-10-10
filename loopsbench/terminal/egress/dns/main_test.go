package main

import (
	"net"
	"testing"

	"golang.org/x/net/dns/dnsmessage"
)

func TestOnlyExactAddressQuestions(t *testing.T) {
	allowed := records{"model.test.": {net.ParseIP("192.0.2.1"), net.ParseIP("2001:db8::1")}}
	for _, tc := range []struct {
		name  string
		kind  dnsmessage.Type
		code  dnsmessage.RCode
		count int
	}{
		{"model.test.", dnsmessage.TypeA, dnsmessage.RCodeSuccess, 1},
		{"MODEL.TEST.", dnsmessage.TypeAAAA, dnsmessage.RCodeSuccess, 1},
		{"model.test.", dnsmessage.TypeTXT, dnsmessage.RCodeRefused, 0},
		{"model.test.", dnsmessage.TypeALL, dnsmessage.RCodeRefused, 0},
		{"data.model.test.", dnsmessage.TypeA, dnsmessage.RCodeRefused, 0},
		{"example.com.", dnsmessage.TypeA, dnsmessage.RCodeRefused, 0},
	} {
		name, _ := dnsmessage.NewName(tc.name)
		query := dnsmessage.Message{Header: dnsmessage.Header{ID: 17, RecursionDesired: true}, Questions: []dnsmessage.Question{{Name: name, Type: tc.kind, Class: dnsmessage.ClassINET}}}
		packet, _ := query.Pack()
		var result dnsmessage.Message
		if err := result.Unpack(answer(packet, allowed)); err != nil {
			t.Fatal(err)
		}
		if result.ID != 17 || !result.Response || result.RCode != tc.code || len(result.Answers) != tc.count || len(result.Additionals) != 0 {
			t.Fatalf("%s %s: unexpected response %+v", tc.name, tc.kind, result)
		}
		query.Questions = append(query.Questions, query.Questions[0])
		packet, _ = query.Pack()
		if err := result.Unpack(answer(packet, allowed)); err != nil || result.RCode != dnsmessage.RCodeFormatError {
			t.Fatal("multi-question accepted")
		}
	}
}

func FuzzDNSPacket(f *testing.F) {
	f.Add([]byte("bad packet"))
	f.Fuzz(func(t *testing.T, packet []byte) { answer(packet, records{}) })
}
