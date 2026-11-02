//===-- cc1_main.cpp - Clang CC1 Compiler Frontend ------------------------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// This is the entry point to the clang -cc1 functionality, which implements the
// core compiler functionality along with a number of additional tools for
// demonstration and testing purposes.
//
//===----------------------------------------------------------------------===//

#include "clang/Basic/DiagnosticFrontend.h"
#include "clang/Basic/Stack.h"
#include "clang/Basic/TargetOptions.h"
#include "clang/CodeGen/ObjectFilePCHContainerWriter.h"
#include "clang/Config/config.h"
#include "clang/Driver/Driver.h"
#include "clang/Driver/DriverDiagnostic.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/CompilerInvocation.h"
#include "clang/Frontend/TextDiagnosticBuffer.h"
#include "clang/Frontend/TextDiagnosticPrinter.h"
#include "clang/Frontend/Utils.h"
#include "clang/FrontendTool/Utils.h"
#include "clang/Options/Options.h"
#include "clang/Serialization/ObjectFilePCHContainerReader.h"
#include "llvm/ADT/Statistic.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/Config/llvm-config.h"
#include "llvm/LinkAllPasses.h"
#include "llvm/MC/MCSubtargetInfo.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Option/Arg.h"
#include "llvm/Option/ArgList.h"
#include "llvm/Option/OptTable.h"
#include "llvm/Support/BuryPointer.h"
#include "llvm/Support/Compiler.h"
#include "llvm/Support/ErrorHandling.h"
#include "llvm/Support/Endian.h"
#include "llvm/Support/IOSandbox.h"
#include "llvm/Support/ManagedStatic.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/Process.h"
#include "llvm/Support/Signals.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/TimeProfiler.h"
#include "llvm/Support/Timer.h"
#include "llvm/Support/VirtualFileSystem.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Target/TargetMachine.h"
#include "llvm/TargetParser/AArch64TargetParser.h"
#include "llvm/TargetParser/ARMTargetParser.h"
#include "llvm/TargetParser/RISCVISAInfo.h"
#include <cstdio>

#ifdef CLANG_HAVE_RLIMITS
#include <sys/resource.h>
#endif

using namespace clang;
using namespace llvm::opt;

//===----------------------------------------------------------------------===//
// Main driver
//===----------------------------------------------------------------------===//

static void LLVMErrorHandler(void *UserData, const char *Message,
                             bool GenCrashDiag) {
  DiagnosticsEngine &Diags = *static_cast<DiagnosticsEngine*>(UserData);

  Diags.Report(diag::err_fe_error_backend) << Message;

  // Run the interrupt handlers to make sure any special cleanups get done, in
  // particular that we remove files registered with RemoveFileOnSignal.
  llvm::sys::RunInterruptHandlers();

  // We cannot recover from llvm errors.  When reporting a fatal error, exit
  // with status 70 to generate crash diagnostics.  For BSD systems this is
  // defined as an internal software error.  Otherwise, exit with status 1.
  llvm::sys::Process::Exit(GenCrashDiag ? 70 : 1);
}

#ifdef CLANG_HAVE_RLIMITS
/// Attempt to ensure that we have at least 8MiB of usable stack space.
static void ensureSufficientStack() {
  struct rlimit rlim;
  if (getrlimit(RLIMIT_STACK, &rlim) != 0)
    return;

  // Increase the soft stack limit to our desired level, if necessary and
  // possible.
  if (rlim.rlim_cur != RLIM_INFINITY &&
      rlim.rlim_cur < rlim_t(DesiredStackSize)) {
    // Try to allocate sufficient stack.
    if (rlim.rlim_max == RLIM_INFINITY ||
        rlim.rlim_max >= rlim_t(DesiredStackSize))
      rlim.rlim_cur = DesiredStackSize;
    else if (rlim.rlim_cur == rlim.rlim_max)
      return;
    else
      rlim.rlim_cur = rlim.rlim_max;

    if (setrlimit(RLIMIT_STACK, &rlim) != 0 ||
        rlim.rlim_cur != DesiredStackSize)
      return;
  }
}
#else
static void ensureSufficientStack() {}
#endif

/// Print supported cpus of the given target.
static int PrintSupportedCPUs(std::string TargetStr) {
  llvm::Triple Triple(TargetStr);
  std::string Error;
  const llvm::Target *TheTarget =
      llvm::TargetRegistry::lookupTarget(Triple, Error);
  if (!TheTarget) {
    llvm::errs() << Error;
    return 1;
  }

  // the target machine will handle the mcpu printing
  llvm::TargetOptions Options;
  std::unique_ptr<llvm::TargetMachine> TheTargetMachine(
      TheTarget->createTargetMachine(Triple, "", "+cpuhelp", Options,
                                     std::nullopt));
  return 0;
}

static int PrintSupportedExtensions(std::string TargetStr) {
  llvm::Triple Triple(TargetStr);
  std::string Error;
  const llvm::Target *TheTarget =
      llvm::TargetRegistry::lookupTarget(Triple, Error);
  if (!TheTarget) {
    llvm::errs() << Error;
    return 1;
  }

  llvm::TargetOptions Options;
  std::unique_ptr<llvm::TargetMachine> TheTargetMachine(
      TheTarget->createTargetMachine(Triple, "", "", Options, std::nullopt));
  const llvm::Triple &MachineTriple = TheTargetMachine->getTargetTriple();
  const llvm::MCSubtargetInfo &MCInfo = TheTargetMachine->getMCSubtargetInfo();
  const llvm::ArrayRef<llvm::SubtargetFeatureKV> Features =
      MCInfo.getAllProcessorFeatures();

  llvm::StringMap<llvm::StringRef> DescMap;
  for (const llvm::SubtargetFeatureKV &feature : Features)
    DescMap.insert({feature.key(), feature.desc()});

  if (MachineTriple.isRISCV())
    llvm::RISCVISAInfo::printSupportedExtensions(DescMap);
  else if (MachineTriple.isAArch64())
    llvm::AArch64::PrintSupportedExtensions();
  else if (MachineTriple.isARM())
    llvm::ARM::PrintSupportedExtensions(DescMap);
  else {
    // The option was already checked in Driver::HandleImmediateArgs,
    // so we do not expect to get here if we are not a supported architecture.
    assert(0 && "Unhandled triple for --print-supported-extensions option.");
    return 1;
  }

  return 0;
}

static int PrintEnabledExtensions(const TargetOptions& TargetOpts) {
  llvm::Triple Triple(TargetOpts.Triple);
  std::string Error;
  const llvm::Target *TheTarget =
      llvm::TargetRegistry::lookupTarget(Triple, Error);
  if (!TheTarget) {
    llvm::errs() << Error;
    return 1;
  }

  // Create a target machine using the input features, the triple information
  // and a dummy instance of llvm::TargetOptions. Note that this is _not_ the
  // same as the `clang::TargetOptions` instance we have access to here.
  llvm::TargetOptions BackendOptions;
  std::string FeaturesStr = llvm::join(TargetOpts.FeaturesAsWritten, ",");
  std::unique_ptr<llvm::TargetMachine> TheTargetMachine(
      TheTarget->createTargetMachine(Triple, TargetOpts.CPU, FeaturesStr,
                                     BackendOptions, std::nullopt));
  const llvm::Triple &MachineTriple = TheTargetMachine->getTargetTriple();
  const llvm::MCSubtargetInfo &MCInfo = TheTargetMachine->getMCSubtargetInfo();

  // Extract the feature names that are enabled for the given target.
  // We do that by capturing the key from the set of SubtargetFeatureKV entries
  // provided by MCSubtargetInfo, which match the '-target-feature' values.
  const std::vector<const llvm::SubtargetFeatureKV *> Features =
      MCInfo.getEnabledProcessorFeatures();
  std::set<llvm::StringRef> EnabledFeatureNames;
  for (const llvm::SubtargetFeatureKV *feature : Features)
    EnabledFeatureNames.insert(feature->key());

  if (MachineTriple.isAArch64())
    llvm::AArch64::printEnabledExtensions(EnabledFeatureNames);
  else if (MachineTriple.isRISCV()) {
    llvm::StringMap<llvm::StringRef> DescMap;
    for (const llvm::SubtargetFeatureKV *feature : Features)
      DescMap.insert({feature->key(), feature->desc()});
    llvm::RISCVISAInfo::printEnabledExtensions(MachineTriple.isArch64Bit(),
                                               EnabledFeatureNames, DescMap);
  } else {
    // The option was already checked in Driver::HandleImmediateArgs,
    // so we do not expect to get here if we are not a supported architecture.
    assert(0 && "Unhandled triple for --print-enabled-extensions option.");
    return 1;
  }

  return 0;
}

// Record physical filesystem queries, including failed candidates which do not
// appear in Clang's make-style dependency file. Wrapping below VFS overlays
// keeps their backing paths visible without treating virtual paths as files.
class ZigCacheFileSystem final : public llvm::vfs::ProxyFileSystem {
  struct Input {
    char Kind;
    uint64_t Size = 0;
    uint64_t Inode = 0;
    int64_t MTime = 0;

    bool agreesWith(const Input &Other) const {
      const bool BothFiles = (Kind == 'f' || Kind == 'r') &&
                             (Other.Kind == 'f' || Other.Kind == 'r');
      return (Kind == Other.Kind || BothFiles) && Size == Other.Size &&
             Inode == Other.Inode && MTime == Other.MTime;
    }
  };
  llvm::StringMap<Input> Inputs;
  bool Changed = false;

  void record(const Twine &Path, Input Value) {
    SmallString<256> Absolute;
    Path.toVector(Absolute);
    if (makeAbsolute(Absolute)) {
      Changed = true;
      return;
    }
    auto Entry = Inputs.try_emplace(Absolute, Value);
    if (!Entry.second) {
      if (!Entry.first->second.agreesWith(Value))
        Changed = true;
      else if (Value.Kind == 'r')
        Entry.first->second.Kind = 'r';
    }
  }

  void recordError(const Twine &Path, std::error_code EC) {
    if (EC == std::errc::no_such_file_or_directory ||
        EC == std::errc::not_a_directory)
      record(Path, {'m'});
  }

  void recordFile(const Twine &Path, const llvm::vfs::Status &Stat,
                  bool Opened = false) {
    if (Stat.isDirectory())
      record(Path, {'d'});
    else if (Stat.isRegularFile())
      record(Path, {Opened ? 'r' : 'f', Stat.getSize(), Stat.getUniqueID().getFile(),
                    std::chrono::duration_cast<std::chrono::nanoseconds>(
                        Stat.getLastModificationTime().time_since_epoch()).count()});
  }

public:
  explicit ZigCacheFileSystem(IntrusiveRefCntPtr<llvm::vfs::FileSystem> FS)
      : ProxyFileSystem(std::move(FS)) {}

  llvm::ErrorOr<llvm::vfs::Status> status(const Twine &Path) override {
    auto Result = getUnderlyingFS().status(Path);
    if (!Result)
      recordError(Path, Result.getError());
    else
      recordFile(Path, *Result);
    return Result;
  }

  bool exists(const Twine &Path) override { return bool(status(Path)); }

  llvm::ErrorOr<std::unique_ptr<llvm::vfs::File>>
  openFileForRead(const Twine &Path) override {
    auto Result = getUnderlyingFS().openFileForRead(Path);
    if (!Result)
      recordError(Path, Result.getError());
    else {
      auto Stat = (*Result)->status();
      if (Stat)
        recordFile(Path, *Stat, true);
    }
    return Result;
  }

  bool write(StringRef Output) {
    if (Changed) {
      llvm::errs() << "filesystem inputs changed while compiling C source\n";
      return false;
    }
    std::error_code EC;
    llvm::raw_fd_ostream OS(Output, EC);
    if (EC) {
      llvm::errs() << "cannot write Zig cache dependencies: " << EC.message() << '\n';
      return false;
    }
    // Preserve the observed metadata, so a file created or removed during
    // compilation cannot turn an old result into a valid cache entry.
    for (const auto &Entry : Inputs) {
      const auto &Input = Entry.second;
      char Header[25] = {};
      Header[0] = Input.Kind;
      llvm::support::endian::write64le(Header + 1, Input.Size);
      llvm::support::endian::write64le(Header + 9, Input.Inode);
      llvm::support::endian::write64le(Header + 17, Input.MTime);
      OS.write(Header, sizeof(Header));
      OS << Entry.first() << '\0';
    }
    OS.close();
    if (OS.has_error()) {
      llvm::errs() << "cannot write Zig cache dependencies\n";
      OS.clear_error();
      return false;
    }
    return true;
  }
};

int cc1_main(ArrayRef<const char *> Argv, const char *Argv0, void *MainAddr) {
  ensureSufficientStack();

  StringRef CacheDependencies;
  SmallVector<const char *, 256> FilteredArgs;
  for (size_t I = 0; I < Argv.size(); ++I) {
    if (StringRef(Argv[I]) == "-zig-cache-dependencies") {
      if (++I == Argv.size()) {
        llvm::errs() << "missing Zig cache dependency output path\n";
        return 1;
      }
      CacheDependencies = Argv[I];
    } else {
      FilteredArgs.push_back(Argv[I]);
    }
  }
  Argv = FilteredArgs;

  IntrusiveRefCntPtr<DiagnosticIDs> DiagID = DiagnosticIDs::create();

  // Register the support for object-file-wrapped Clang modules.
  auto PCHOps = std::make_shared<PCHContainerOperations>();
  PCHOps->registerWriter(std::make_unique<ObjectFilePCHContainerWriter>());
  PCHOps->registerReader(std::make_unique<ObjectFilePCHContainerReader>());

  // Initialize targets first, so that --version shows registered targets.
  llvm::InitializeAllTargets();
  llvm::InitializeAllTargetMCs();
  llvm::InitializeAllAsmPrinters();
  llvm::InitializeAllAsmParsers();

  // Buffer diagnostics from argument parsing so that we can output them using a
  // well formed diagnostic object.
  DiagnosticOptions DiagOpts;
  TextDiagnosticBuffer *DiagsBuffer = new TextDiagnosticBuffer;
  DiagnosticsEngine Diags(DiagID, DiagOpts, DiagsBuffer);

  // Setup round-trip remarks for the DiagnosticsEngine used in CreateFromArgs.
  if (find(Argv, StringRef("-Rround-trip-cc1-args")) != Argv.end())
    Diags.setSeverity(diag::remark_cc1_round_trip_generated,
                      diag::Severity::Remark, {});

  auto Invocation = std::make_shared<CompilerInvocation>();
  bool Success =
      CompilerInvocation::CreateFromArgs(*Invocation, Argv, Diags, Argv0);

  auto Clang = std::make_unique<CompilerInstance>(std::move(Invocation),
                                                  std::move(PCHOps));

  if (!Clang->getFrontendOpts().TimeTracePath.empty()) {
    llvm::timeTraceProfilerInitialize(
        Clang->getFrontendOpts().TimeTraceGranularity, Argv0,
        Clang->getFrontendOpts().TimeTraceVerbose);
  }
  // --print-supported-cpus takes priority over the actual compilation.
  if (Clang->getFrontendOpts().PrintSupportedCPUs)
    return PrintSupportedCPUs(Clang->getTargetOpts().Triple);

  // --print-supported-extensions takes priority over the actual compilation.
  if (Clang->getFrontendOpts().PrintSupportedExtensions)
    return PrintSupportedExtensions(Clang->getTargetOpts().Triple);

  // --print-enabled-extensions takes priority over the actual compilation.
  if (Clang->getFrontendOpts().PrintEnabledExtensions)
    return PrintEnabledExtensions(Clang->getTargetOpts());

  // Infer the builtin include path if unspecified.
  if (Clang->getHeaderSearchOpts().UseBuiltinIncludes &&
      Clang->getHeaderSearchOpts().ResourceDir.empty())
    Clang->getHeaderSearchOpts().ResourceDir =
        GetResourcesPath(Argv0, MainAddr);

  /// Create the actual file system.
  auto VFS = [] {
    auto BypassSandbox = llvm::sys::sandbox::scopedDisable();
    return llvm::vfs::getRealFileSystem();
  }();
  IntrusiveRefCntPtr<ZigCacheFileSystem> CacheFS;
  if (!CacheDependencies.empty()) {
    CacheFS = new ZigCacheFileSystem(std::move(VFS));
    VFS = CacheFS;
  }
  Clang->createVirtualFileSystem(std::move(VFS), DiagsBuffer);

  // Create the actual diagnostics engine.
  Clang->createDiagnostics();

  // Set an error handler, so that any LLVM backend diagnostics go through our
  // error handler.
  llvm::install_fatal_error_handler(LLVMErrorHandler,
                                  static_cast<void*>(&Clang->getDiagnostics()));

  DiagsBuffer->FlushDiagnostics(Clang->getDiagnostics());
  if (!Success)
    return 1;

  // Execute the frontend actions.
  Success = ExecuteCompilerInvocation(Clang.get());

  if (Success && CacheFS)
    Success = CacheFS->write(CacheDependencies);

  // If any timers were active but haven't been destroyed yet, print their
  // results now.  This happens in -disable-free mode.
  {
    // This isn't a formal input or output of the compiler.
    auto BypassSandbox = llvm::sys::sandbox::scopedDisable();
    std::unique_ptr<raw_ostream> IOFile = llvm::CreateInfoOutputFile();
    if (Clang->getCodeGenOpts().TimePassesJson) {
      *IOFile << "{\n";
      llvm::TimerGroup::printAllJSONValues(*IOFile, "");
      *IOFile << "\n}\n";
    } else if (!Clang->getCodeGenOpts().TimePassesStatsFile) {
      llvm::TimerGroup::printAll(*IOFile);
    }
    llvm::TimerGroup::clearAll();
  }

  if (llvm::timeTraceProfilerEnabled()) {
    if (auto profilerOutput = Clang->createOutputFile(
            Clang->getFrontendOpts().TimeTracePath, /*Binary=*/false,
            /*RemoveFileOnSignal=*/false,
            /*useTemporary=*/false)) {
      llvm::timeTraceProfilerWrite(*profilerOutput);
      profilerOutput.reset();
      llvm::timeTraceProfilerCleanup();
      Clang->clearOutputFiles(false);
    }
  }

  // Our error handler depends on the Diagnostics object, which we're
  // potentially about to delete. Uninstall the handler now so that any
  // later errors use the default handling behavior instead.
  llvm::remove_fatal_error_handler();

  // When running with -disable-free, don't do any destruction or shutdown.
  if (Clang->getFrontendOpts().DisableFree) {
    // DiagnosticConsumer must be always destroyed.
    Clang->getDiagnosticClient().~DiagnosticConsumer();
    llvm::BuryPointer(std::move(Clang));
    return !Success;
  }

  return !Success;
}
