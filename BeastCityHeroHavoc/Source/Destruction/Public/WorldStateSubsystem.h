// Persists destroyed-building state across sessions/reloads. GameInstance-scoped so state
// survives level travel within one play session, and is written to/read from a disk slot.
#pragma once

#include "CoreMinimal.h"
#include "Subsystems/GameInstanceSubsystem.h"
#include "WorldStateSubsystem.generated.h"

UCLASS()
class DESTRUCTION_API UWorldStateSubsystem : public UGameInstanceSubsystem
{
	GENERATED_BODY()

public:
	static const TCHAR* SaveSlotName;

	// Marks BuildingId destroyed in the in-memory set (does not touch disk).
	UFUNCTION(BlueprintCallable, Category = "World|Save")
	void MarkBuildingDestroyed(FName BuildingId);

	UFUNCTION(BlueprintPure, Category = "World|Save")
	bool WasBuildingDestroyed(FName BuildingId) const;

	UFUNCTION(BlueprintCallable, Category = "World|Save")
	bool SaveWorldState(FString& OutError);

	UFUNCTION(BlueprintCallable, Category = "World|Save")
	bool LoadWorldState(FString& OutError);

	UFUNCTION(BlueprintCallable, Category = "World|Save")
	void ResetWorldState();

private:
	UPROPERTY()
	TSet<FName> DestroyedBuildingIds;
};
